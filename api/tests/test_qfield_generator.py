"""Unit tests — app.qfield generator (decision-generate-field-project.md).

Pure-logic tests in the house shape (no app instance, no DB): compile ProjectSpecs and
assert on the emitted XML/zip, including the §F7 self-check — our own output must pass our
own intake lip (app.field_projects.inspect_qgz), the DOCTYPE lesson made permanent.
"""
import io
import os
import re
import tempfile
import zipfile

import pytest

from app import field_projects as fp
from app.qfield import GenerationError, LayerSpec, ProjectSpec, compile_project
from app.routers.field_projects import _hex_or_default, _merged_warnings, _resolve_renderer

ORIGIN = "https://talus.example.org"
PAT = "tpat_UNITTESTTOKEN"


def _point(layer_id="tal_0a1b2c3d", name="Signs", attachments=False, fields=None, **kw):
    fields = fields if fields is not None else ["OBJECTID", "GlobalID", "notes", "gnss_fix_time"]
    if attachments and "_talus_attach" not in fields:
        fields = fields + ["_talus_attach"]
    return LayerSpec(layer_id, name, "point", fields, attachments=attachments, **kw)


def _compile(*layers, title="Test field", center=(-82.5, 27.3), zoom=15):
    return compile_project(ProjectSpec(
        title=title, origin=ORIGIN, pat=PAT, layers=list(layers), center=center, zoom=zoom,
    ))


def _doc(data: bytes) -> str:
    zf = zipfile.ZipFile(io.BytesIO(data))
    (member,) = zf.namelist()
    assert member.endswith(".qgs")
    return zf.read(member).decode()


def test_multi_geometry_compile_and_selfcheck():
    data, warns = _compile(
        _point(editable=True, attachments=True, color="#2563eb"),
        LayerSpec("tal_11112222", "Sidewalks", "line", ["OBJECTID", "GlobalID"],
                  editable=True, color="#dc2626", line_width=1.2, line_dash=[4, 2]),
        LayerSpec("tal_33334444", "Parks", "polygon", ["OBJECTID", "GlobalID"],
                  color="#16a34a", fill_opacity=0.25, outline_color="#065f46"),
        title='Signs & "Trees" field',
    )
    doc = _doc(data)
    assert warns == []
    # one maplayer per vector layer + the OSM basemap
    assert doc.count("<maplayer") == 4
    for needle in ('geometry="Point"', 'geometry="Line"', 'geometry="Polygon"',
                   'wkbType="MultiLineString"', 'wkbType="MultiPolygon"',
                   'class="SimpleMarker"', 'class="SimpleLine"', 'class="SimpleFill"'):
        assert needle in doc, needle
    # the PAT rides the URL userinfo in every hosted datasource
    assert f"talus:{PAT}@talus.example.org" in doc
    assert "{{" not in doc
    # title lands in BOTH sniffable homes, escaped
    assert '<title>Signs &amp; "Trees" field</title>' in doc
    assert "projectname=" in doc.splitlines()[1]
    # line dash rides through as a custom dash
    assert '<Option type="QString" value="1" name="use_custom_dash"/>' in doc
    assert '<Option type="QString" value="4;2" name="customdash"/>' in doc

    # §F7 self-check: the same lip every upload passes.
    fd, path = tempfile.mkstemp(suffix=".qgz")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        info = fp.inspect_qgz(path, "talus.example.org")
    finally:
        os.unlink(path)
    assert info.qgs_title == 'Signs & "Trees" field'
    assert info.layer_count == 4
    assert info.warnings == []   # notably: no foreign_datasources, no unreadable_metadata


def test_attachment_kit_gating_and_naming():
    # gated ON: widget + gnss_fix_time naming
    data, warns = _compile(_point(attachments=True, editable=True))
    doc = _doc(data)
    assert warns == []
    assert doc.count('name="_talus_attach"') == 1
    assert '<Option name="StorageType" value="WebDAV" type="QString"/>' in doc
    assert '<Option name="StorageAuthConfigId" value="" type="QString"/>' in doc
    assert "gnss_fix_time" in doc and "NOFIX" in doc
    # gated ON but no gnss_fix_time: wall-clock naming + the warning code
    data, warns = _compile(_point(attachments=True, fields=["OBJECTID", "GlobalID", "_talus_attach"]))
    doc = _doc(data)
    assert "photo_naming_wall_clock" in warns
    assert "format_date(now()" in doc
    # gated OFF: no widget, empty naming map
    data, warns = _compile(_point(attachments=False))
    doc = _doc(data)
    assert '_talus_attach' not in doc
    assert '<Option type="QString" value="{}" name="QFieldSync/attachment_naming"/>' in doc


def test_gnss_defaults_only_for_present_fields_and_constraints_zeroed():
    data, _ = _compile(_point(fields=["OBJECTID", "GlobalID", "gnss_pdop", "notes"]))
    doc = _doc(data)
    assert '@position_pdop' in doc
    assert '@position_horizontal_accuracy' not in doc
    # every field constraint zeroed — the template's OBJECTID unique/notnull must not survive
    assert 'unique_strength="1"' not in doc
    frag = doc.split("<constraints>")[1].split("</constraints>")[0]
    assert frag.count("<constraint ") == 4


def test_gps_destination_prefers_editable_point():
    data, _ = _compile(
        LayerSpec("tal_11112222", "Sidewalks", "line", ["OBJECTID"], editable=True, color="#dc2626"),
        _point(name="Hydrants", editable=True),
    )
    doc = _doc(data)
    m = re.search(r"<ProjectGpsSettings destinationLayerName=(['\"])(.*?)\1", doc)
    assert m and m.group(2) == "Hydrants"
    # no editable point at all -> first layer is the destination
    data, _ = _compile(
        LayerSpec("tal_11112222", "Sidewalks", "line", ["OBJECTID"], editable=True, color="#dc2626"),
    )
    m = re.search(r"<ProjectGpsSettings destinationLayerName=(['\"])(.*?)\1", _doc(data))
    assert m and m.group(2) == "Sidewalks"


def test_generation_errors():
    with pytest.raises(GenerationError):
        compile_project(ProjectSpec(title="x", origin=ORIGIN, pat=PAT, layers=[]))
    with pytest.raises(GenerationError):
        _compile(_point(color="not-a-color"))
    with pytest.raises(GenerationError):
        _compile(LayerSpec("tal_1", "X", "multipatch", ["OBJECTID"]))
    with pytest.raises(GenerationError):
        compile_project(ProjectSpec(title="x", origin="ftp://nope", pat=PAT, layers=[_point()]))


def test_resolve_renderer_vocabulary():
    # simple -> the symbol color rides through
    color, style, flat, icon = _resolve_renderer("#111111", {"type": "simple", "symbol": {"color": "#a1b2c3"}})
    assert (color, flat, icon) == ("#a1b2c3", False, False) and style == {}
    # uniqueValue -> default color wins, flattened flagged (F3 as endorsed)
    color, _, flat, _icon = _resolve_renderer("#111111", {
        "type": "uniqueValue", "field": "kind",
        "stops": [{"value": "a", "color": "#0000ff"}], "default": {"color": "#00ff00"},
    })
    assert (color, flat) == ("#00ff00", True)
    # uniqueValue without default -> first stop
    color, _, flat, _icon = _resolve_renderer("#111111", {"type": "uniqueValue", "stops": [{"color": "#0000ff"}]})
    assert (color, flat) == ("#0000ff", True)
    # no renderer -> display_color; garbage -> the default blue
    assert _resolve_renderer("#123456", None)[0] == "#123456"
    assert _resolve_renderer("nonsense", None)[0] == "#2563eb"
    # layer-wide style keys map to LayerSpec kwargs
    _, style, _, _ = _resolve_renderer("#111111", {
        "type": "simple", "symbol": {"color": "#a1b2c3"},
        "lineWidth": 2, "lineDash": [5, 2], "fillOpacity": 0.5,
        "outlineColor": "#0a0b0c", "outlineWidth": 0.6,
    })
    assert style == {"line_width": 2, "line_dash": [5, 2], "fill_opacity": 0.5,
                     "outline_color": "#0a0b0c", "outline_width": 0.6}


def test_resolve_renderer_point_size_and_icon():
    """decision-point-icons.md fork E: size travels (px diameter -> QGIS points), the ICON does not
    — it is flattened away and flagged, so the crew is told rather than shown a wrong symbol."""
    _, style, _, icon_dropped = _resolve_renderer("#111111", {
        "type": "simple", "symbol": {"color": "#a1b2c3"}, "pointSize": 24, "pointIcon": "fire_hydrant",
    })
    # 24 px diameter * 0.75 = 18 pt — a diameter throughout, never a radius (the x2 trap).
    assert style == {"point_size": 18.0}
    assert icon_dropped is True
    # no icon -> no warn
    _, _, _, icon_dropped = _resolve_renderer("#111111", {
        "type": "simple", "symbol": {"color": "#a1b2c3"}, "pointSize": 24,
    })
    assert icon_dropped is False


def test_helpers():
    assert _hex_or_default("#aabbcc") == "#aabbcc"
    assert _hex_or_default("#zzz") == "#2563eb"
    assert _hex_or_default(None, "#000000") == "#000000"
    assert _merged_warnings(["a", "b"], ["b", "c"], []) == ["a", "b", "c"]


def test_default_view_extent_is_the_projects_own_not_a_fixed_place():
    """Once-over P2: the template carried a literal <DefaultViewExtent> — one fixed place on the map
    for every customer's generated project, and never rewritten by the generator. It must be the same
    extent as the canvas, computed from this project's centre and zoom."""
    import re

    doc = _doc(_compile(_point(editable=True), center=(10.0, 50.0), zoom=12)[0])
    assert "{{" not in doc, "an unreplaced placeholder survived"
    m = re.search(r'<DefaultViewExtent ymin="([^"]+)" ymax="([^"]+)" xmin="([^"]+)" xmax="([^"]+)">', doc)
    assert m, "no DefaultViewExtent in the generated project"
    ymin, ymax, xmin, xmax = (float(v) for v in m.groups())
    c = re.search(r"<extent>\s*<xmin>([^<]+)</xmin>\s*<ymin>([^<]+)</ymin>\s*<xmax>([^<]+)</xmax>\s*<ymax>([^<]+)</ymax>", doc)
    assert c, "no canvas extent in the generated project"
    cxmin, cymin, cxmax, cymax = (float(v) for v in c.groups())
    assert (xmin, ymin, xmax, ymax) == (cxmin, cymin, cxmax, cymax)
    assert xmin < 10.0 < xmax and ymin < 50.0 < ymax, "the default view does not contain the project's centre"
    # the old literal, and anything near it, is gone
    assert "27.2926" not in doc and "-82.6007" not in doc


def test_the_template_holds_no_literal_default_view():
    from app.qfield import generator

    shell = (generator._TEMPLATES / "shell.qgs.tmpl").read_text()
    line = next(ln for ln in shell.splitlines() if "<DefaultViewExtent" in ln)
    assert line.strip() == '<DefaultViewExtent ymin="{{YMIN}}" ymax="{{YMAX}}" xmin="{{XMIN}}" xmax="{{XMAX}}">'
