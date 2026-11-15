"""Export truncation must never be silent (decision-export-truncation.md).

Found 2026-08-21: a 330,645-row layer exported as exactly 250,000 features with no
client-visible signal — a complete-looking file quietly missing 80k rows, on a product whose
pitch is "your data is yours to take elsewhere". These guards pin the boundary and the three
surfaces the signal rides (headers · filename · in-format).
"""
from __future__ import annotations

import io
import zipfile

from app.routers.layer_query import _EXPORT_CAP, _export_headers, _truncation_meta


# --- the boundary (mutation-checked: flipping >= to > must fail test_at_cap) ----------------------

def test_below_cap_is_not_truncated():
    assert _truncation_meta(0, cap=10) is None
    assert _truncation_meta(9, cap=10) is None


def test_at_cap_reports_truncated():
    """LIMIT cap returning exactly cap means we hit the ceiling — the deliberate over-warn
    (§3): a layer with exactly cap rows is reported truncated rather than paying for a
    second COUNT(*) scan on every download."""
    meta = _truncation_meta(10, cap=10)
    assert meta == {"returned": 10, "cap": 10}


def test_above_cap_reports_truncated():
    assert _truncation_meta(11, cap=10) == {"returned": 11, "cap": 10}


def test_default_cap_is_the_module_constant():
    assert _truncation_meta(_EXPORT_CAP) == {"returned": _EXPORT_CAP, "cap": _EXPORT_CAP}
    assert _truncation_meta(_EXPORT_CAP - 1) is None


# --- surface 1+2: headers and the filename marker -------------------------------------------------

def test_complete_export_carries_no_truncation_headers():
    """The negative control — a signal that always fires is not a signal."""
    h = _export_headers("tal_x-20260821", "csv", None)
    assert h["Content-Disposition"] == 'attachment; filename="tal_x-20260821.csv"'
    assert not any(k.startswith("X-Talus-Export") for k in h)


def test_truncated_export_marks_the_filename_and_sets_headers():
    h = _export_headers("tal_x-20260821", "gpkg", {"returned": 250000, "cap": 250000})
    assert h["Content-Disposition"] == 'attachment; filename="tal_x-20260821-TRUNCATED-at-250000.gpkg"'
    assert h["X-Talus-Export-Truncated"] == "true"
    assert h["X-Talus-Export-Returned"] == "250000"
    assert h["X-Talus-Export-Cap"] == "250000"


def test_filename_marker_applies_to_every_format():
    for ext in ("csv", "geojson", "zip", "gpkg"):
        h = _export_headers("tal_x-20260821", ext, {"returned": 5, "cap": 5})
        assert h["Content-Disposition"].endswith(f'-TRUNCATED-at-5.{ext}"')


def test_truncation_headers_are_cors_exposed():
    """A header the browser hides from the SPA is not a signal (the SPA is cross-origin in dev)."""
    from app.main import app

    exposed = next(
        set(m.kwargs["expose_headers"]) for m in app.user_middleware
        if "expose_headers" in getattr(m, "kwargs", {})
    )
    assert {"X-Talus-Export-Truncated", "X-Talus-Export-Returned", "X-Talus-Export-Cap",
            "Content-Disposition"} <= exposed


# --- surface 3: in-format signals -----------------------------------------------------------------

def test_geojson_foreign_members_shape():
    """RFC 7946 permits foreign members; they keep the truth attached to the FILE. This mirrors
    the payload assembly in _export_geojson (kept in lockstep by the live leg)."""
    payload = {"type": "FeatureCollection", "features": []}
    meta = {"returned": 250000, "cap": 250000}
    payload["talus:truncated"] = True
    payload["talus:returned"] = meta["returned"]
    payload["talus:cap"] = meta["cap"]
    assert payload["talus:truncated"] is True
    assert payload["type"] == "FeatureCollection"  # still a valid FeatureCollection


def test_shapefile_zip_readme_is_readable_and_names_the_numbers():
    meta = {"returned": 250000, "cap": 250000}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("tal_x.shp", b"")
        zf.writestr(
            "TRUNCATED-README.txt",
            f"INCOMPLETE EXPORT\n\nThis shapefile contains {meta['returned']} features — the "
            f"server's per-export\nceiling of {meta['cap']}.\n",
        )
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        assert "TRUNCATED-README.txt" in zf.namelist()
        text = zf.read("TRUNCATED-README.txt").decode()
    assert "INCOMPLETE EXPORT" in text and "250000" in text
