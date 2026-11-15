"""Shared readers for uploaded geodata files (decision-add-from-file.md §4.1).

Used by BOTH file-import surfaces — the feature-ingest endpoint (routers/layer_features.py) and the
create-wizard schema preview (routers/layer_fields.py) — so the two can never drift apart on how a
file is parsed: one zip/member-pick, one .prj→EPSG resolution, one CSV coordinate-column detection.
Everything reads fully in-memory; no GDAL, no temp files.
"""

import io
import re
import zipfile
import zlib

from fastapi import HTTPException, status

# One cap across the whole add-from-file flow: preview AND ingest, all formats (decision-add-from-file.md
# §2.5). Decoded bytes for base64 payloads, text length for geojson_text/csv_text.
MAX_FILE_BYTES = 50 * 1024 * 1024
# Bounded synchronous PER-REQUEST batch; streaming/async is a later scope. Sized for the formats the
# browser can't split (a zipped shapefile / gpkg — a municipal street centerline file is ~35k features);
# GeoJSON/CSV clients chunk BELOW this instead (decision-add-from-file.md §2.5). Surfaced by the preview
# so the wizard can warn before Finish.
INGEST_FEATURE_CAP = 50000

# Geometry family by GeoJSON type. MultiPoint/MultiLineString/MultiPolygon fold into their single-part
# family; GeometryCollection (and anything else) is unsupported → None.
FAMILY_BY_GEOJSON = {
    "Point": "point", "MultiPoint": "point",
    "LineString": "line", "MultiLineString": "line",
    "Polygon": "polygon", "MultiPolygon": "polygon",
}

# Geometry family by shapefile shape-type code (plain + Z/M variants; 31 = MultiPatch is unsupported).
FAMILY_BY_SHPTYPE = {
    1: "point", 8: "point", 11: "point", 18: "point", 21: "point", 28: "point",
    3: "line", 13: "line", 23: "line",
    5: "polygon", 15: "polygon", 25: "polygon",
}

# Geometry family by gpkg_geometry_columns.geometry_type_name (lowercased). The generic 'geometry'
# type declares no family → None (the caller falls back to letting the user pick).
FAMILY_BY_GPKG_NAME = {
    "point": "point", "multipoint": "point",
    "linestring": "line", "multilinestring": "line",
    "polygon": "polygon", "multipolygon": "polygon",
}


def srid_from_prj(wkt: str | None) -> int | None:
    """Best-effort srid from a Shapefile `.prj` (WKT). The CRS's own AUTHORITY is the LAST EPSG one (inner
    AUTHORITYs belong to the datum/spheroid). ESRI WKT often omits AUTHORITY entirely — then we recognise plain
    WGS-84 geographic (very common) and otherwise return None so the caller asks the user to declare the CRS."""
    if not wkt:
        return None
    codes = re.findall(r'AUTHORITY\s*\[\s*"EPSG"\s*,\s*"?(\d+)"?\s*\]', wkt, re.IGNORECASE)
    if codes:
        return int(codes[-1])
    if "PROJCS" not in wkt.upper() and re.search(r'GCS_WGS_1984|"WGS[ _]84"|D_WGS_1984', wkt, re.IGNORECASE):
        return 4326
    return None


# The small import and the schema preview read a zipped Shapefile into memory. The 50 MB cap applies
# to the upload; a member can expand far beyond it, so what is unpacked is counted while it is read,
# never trusted from the zip's headers (once-over 2026-11-15 K5). The same ceiling as GeoParquet's.
SHAPEFILE_UNPACKED_CEILING = 256 * 1024 * 1024
_UNPACK_CHUNK = 1024 * 1024


def _read_members_capped(zf: zipfile.ZipFile, names: list[str | None], ceiling: int) -> list[bytes | None]:
    """Read the named members whole, stopping with 413 once their unpacked total passes `ceiling`."""
    out: list[bytes | None] = []
    total = 0
    for name in names:
        if name is None:
            out.append(None)
            continue
        parts: list[bytes] = []
        with zf.open(name) as src:
            while True:
                chunk = src.read(_UNPACK_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > ceiling:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"the Shapefile unpacks to more than {ceiling // (1024 * 1024)} MB; "
                               "use Import data for large files",
                    )
                parts.append(chunk)
        out.append(b"".join(parts))
    return out


def open_shapefile_zip(data: bytes):
    """A zipped .shp/.dbf/.shx/.prj → (pyshp Reader, detected_srid_or_None), fully in-memory. Raises the
    same 422s both endpoints used to raise for a bad zip / missing .shp / unreadable shapefile. The srid is
    parsed from the .prj (None when it can't be — the caller then requires the user to declare the CRS)."""
    import shapefile  # lazy: only these paths need pyshp (already a dep, used by export)

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Shapefile must be a .zip")
    names = zf.namelist()

    def pick(ext: str) -> str | None:
        return next((n for n in names if n.lower().endswith(ext) and not n.endswith("/")), None)

    shp_n, dbf_n, shx_n, prj_n = pick(".shp"), pick(".dbf"), pick(".shx"), pick(".prj")
    if not shp_n:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no .shp found in the zip")
    try:
        prj_b, shp_b, dbf_b, shx_b = _read_members_capped(zf, [prj_n, shp_n, dbf_n, shx_n], SHAPEFILE_UNPACKED_CEILING)
    except (zipfile.BadZipFile, zlib.error, OSError, RuntimeError, EOFError, NotImplementedError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="could not read the Shapefile")
    detected = srid_from_prj(prj_b.decode("utf-8", "replace") if prj_b is not None else None)
    try:
        reader = shapefile.Reader(
            shp=io.BytesIO(shp_b),
            dbf=io.BytesIO(dbf_b) if dbf_b is not None else None,
            shx=io.BytesIO(shx_b) if shx_b is not None else None,
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="could not read the Shapefile")
    return reader, detected


# CSV coordinate-column names (case-insensitive, priority order within each family). Geographic names imply
# WGS-84; projected names (x/y/easting/northing) carry no CRS, so the user must declare a source_srid (the same
# path as a Shapefile .prj with no AUTHORITY).
#
# ★ The two axes are resolved BY FAMILY, never independently. A header with `Longitude` but no `Latitude` that
# also carries `x`/`y` must resolve to the projected pair (declare), not to `Longitude` + `y` — a mixed pair
# holds degrees on one axis and linear units on the other, so no single SRID describes it, and the declared
# projected SRID lands the points in the ocean with no error raised (backlog, filed 2026-09-11). The bulk-import
# door (`import_jobs_service._csv_xy`) already resolves by family; this is the same rule for the wizard doors.
CSV_X_GEOGRAPHIC = ["longitude", "lon", "lng", "long"]
CSV_Y_GEOGRAPHIC = ["latitude", "lat"]
CSV_X_PROJECTED = ["x", "easting"]
CSV_Y_PROJECTED = ["y", "northing"]
CSV_GEOGRAPHIC = set(CSV_X_GEOGRAPHIC + CSV_Y_GEOGRAPHIC)
CSV_PROJECTED = set(CSV_X_PROJECTED + CSV_Y_PROJECTED)


def csv_pair_srid(xcol: str, ycol: str) -> int | None:
    """The ONE place that decides what a coordinate-column pair means: 4326 when both names are geographic,
    None (the caller must declare a source_srid) otherwise. Shared by the auto-detect below and by the ingest's
    manual-override branch, so the two cannot drift apart again.

    A MIXED pair — one geographic name, the other a projected one — is refused (422 with the reason): the two
    columns are in different units and no SRID can be right for both. A geographic name paired with a column in
    NEITHER list is not mixed: the user may have picked `Longitude` + `Lat_DD` by hand, which is exactly what the
    manual pick is for — it falls to None (declare), and the ingest's coordinate-range check then judges the
    declared CRS against the actual numbers."""
    xl, yl = xcol.lower(), ycol.lower()
    x_geo, y_geo = xl in CSV_GEOGRAPHIC, yl in CSV_GEOGRAPHIC
    x_proj, y_proj = xl in CSV_PROJECTED, yl in CSV_PROJECTED
    if (x_geo and y_proj) or (x_proj and y_geo):
        geo, proj = (xcol, ycol) if x_geo else (ycol, xcol)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"coordinate columns disagree: {geo!r} is a longitude/latitude name but {proj!r} is a projected "
                "x/y name, so the two are in different units and no single coordinate system fits both — "
                "pick a matching pair (lon/lat, or x/y)"
            ),
        )
    return 4326 if (x_geo and y_geo) else None


def find_csv_coordinates(headers: list[str]) -> tuple[str | None, str | None, int | None]:
    """Detect the coordinate columns in a data-CSV header by common names → (xcol, ycol, detected_srid).
    Family-first: a complete geographic pair wins (⇒ 4326), else a complete projected pair (⇒ None, declare).
    Either column may be None (caller decides: the ingest 422s, the preview returns null candidates so the
    wizard offers a manual pick) — and a partial answer is always from ONE family, never a mixed pair."""
    lower = {h.lower(): h for h in headers if h is not None}

    def find(names: list[str]) -> str | None:
        return next((lower[n] for n in names if n in lower), None)

    geo = find(CSV_X_GEOGRAPHIC), find(CSV_Y_GEOGRAPHIC)
    proj = find(CSV_X_PROJECTED), find(CSV_Y_PROJECTED)
    if all(geo):
        xcol, ycol = geo
    elif all(proj):
        xcol, ycol = proj
    else:
        # Neither family is complete: hand back the geographic partial if there is one (the wizard pre-fills the
        # one column it can), else the projected partial. Never a column from each.
        xcol, ycol = geo if any(geo) else proj
    if not xcol or not ycol:
        return xcol, ycol, None
    return xcol, ycol, csv_pair_srid(xcol, ycol)
