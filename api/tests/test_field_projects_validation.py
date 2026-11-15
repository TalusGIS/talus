"""Field-project upload lip + sniff (V085, decision-field-projects.md F5/F6) — pure logic, no
stack needed. The real-world case is load-bearing: every genuine QGIS project opens with
`<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>`, and the first shipped guard
refused any DOCTYPE — blanking the sniff on every legitimate upload (caught by the acceptance
fixture, the generator's own output). The boundary is entity DECLARATIONS, not the DOCTYPE."""
import io
import zipfile

import pytest

from app.field_projects import NotAFieldProject, inspect_qgz

# The genuine QGIS shape: DOCTYPE + projectname + layers.
_REAL_QGS = (
    "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
    '<qgis projectname="Signs collection" version="3.44.12-Solothurn">'
    "<projectlayers>"
    "<maplayer><datasource>https://portal.example.gov:8443/api/v1/esri-auth/rest/services/tal_da643448/FeatureServer/0</datasource></maplayer>"
    "<maplayer><datasource>type=xyz&amp;url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png</datasource></maplayer>"
    "</projectlayers></qgis>"
)


def _qgz(members: dict[str, str], tmp_path) -> str:
    path = str(tmp_path / "fixture.qgz")
    with zipfile.ZipFile(path, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)
    return path


def test_real_qgis_doctype_sniffs_clean(tmp_path):
    path = _qgz({"project.qgs": _REAL_QGS}, tmp_path)
    info = inspect_qgz(path, expected_origin_host="portal.example.gov")
    assert info.qgs_title == "Signs collection"
    assert info.layer_count == 2
    assert info.warnings == []  # DOCTYPE alone must NOT trip unreadable_metadata


def test_entity_declaration_refused(tmp_path):
    evil = (
        "<!DOCTYPE qgis [<!ENTITY a 'aaaaaaaaaa'>]>"
        '<qgis projectname="x"><projectlayers><maplayer/></projectlayers></qgis>'
    )
    path = _qgz({"project.qgs": evil}, tmp_path)
    info = inspect_qgz(path, expected_origin_host=None)
    assert "unreadable_metadata" in info.warnings
    assert info.qgs_title is None  # never parsed


def test_foreign_datasources_warn(tmp_path):
    path = _qgz({"project.qgs": _REAL_QGS}, tmp_path)
    info = inspect_qgz(path, expected_origin_host="other-portal.example.org")
    assert "foreign_datasources" in info.warnings
    # And skipped entirely when no origin is configured (the server never guesses its hostname).
    assert "foreign_datasources" not in inspect_qgz(path, expected_origin_host=None).warnings


def test_multiple_qgs_and_os_junk_warn_not_block(tmp_path):
    path = _qgz(
        {
            "project.qgs": _REAL_QGS,
            "project.qgs~": "stale pre-conversion backup",
            "__MACOSX/._project.qgs": "appledouble",
        },
        tmp_path,
    )
    info = inspect_qgz(path, expected_origin_host=None)
    assert "multiple_qgs" in info.warnings
    assert "os_junk" in info.warnings
    assert info.qgs_title == "Signs collection"  # junk never picked as the sniff target


def test_hard_floor_rejections(tmp_path):
    not_zip = str(tmp_path / "not.qgz")
    with open(not_zip, "wb") as f:
        f.write(b"plain bytes, not a zip")
    with pytest.raises(NotAFieldProject):
        inspect_qgz(not_zip, expected_origin_host=None)

    no_qgs = _qgz({"readme.txt": "no project here"}, tmp_path)
    with pytest.raises(NotAFieldProject):
        inspect_qgz(no_qgs, expected_origin_host=None)
