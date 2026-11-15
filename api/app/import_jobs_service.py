"""Bulk-import jobs — staging, archive validation, schema sniff, and job CRUD (9d).

decision-9d-bulk-import.md §3-§5. The TRANSPORT half of the door: the router streams the
upload here, we jail it under a per-job staging directory, validate archives (zip-slip and
decompressed-size budget closed BEFORE any parse), sniff the schema cheaply for target=new
provisioning, and keep run-level state in platform.import_jobs (V089). The worker
(import_batch_worker.py) owns the parse/insert half.

Security posture (decision doc §8): staged paths derive from the job id only — a client
filename is DISPLAY DATA, never a path component. Zip entries are validated by name and
type before a byte is extracted; the decompressed budget is enforced during streaming copy
(header sizes lie; the running count doesn't). Sniffers must 422 on malformed input, never
500 (the P6 precedent).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from typing import Any
from uuid import UUID

import structlog
from fastapi import HTTPException, status
from psycopg import AsyncConnection

from . import gpkg_guard
from .config import settings
from .file_ingest import srid_from_prj

log = structlog.get_logger()

# v1 formats (the DB CHECK mirrors this — V089 → V099 → V106)
FORMATS = ("shp-zip", "gpkg", "geojson", "csv", "kml")
# The staged upload's filename per format — ONE mapping, shared by the router (which writes the
# file) and process_job (which reads it back). Found the hard way on 2026-09-17: the worker had its
# own inline copy, 'kml' was added to FORMATS and the router's copy only, and the first live KMZ
# job failed with a bare KeyError('kml') after the layer had already been provisioned. Anything
# keyed by format lives here, next to FORMATS, and test_import_kml pins the two sets equal.
STAGED_EXT = {"shp-zip": ".zip", "gpkg": ".gpkg", "geojson": ".geojson", "csv": ".csv", "kml": ".kml"}

# CSV coordinate-column detection: matched case-insensitively against
# the header. Exactly one match per axis or the sniff rejects with the headers it saw. The name
# FAMILY also decides the CRS default: lat/lon-family ⇒ 4326 (override wins); x/y-family ⇒ no
# default, so the router's existing "supply source_srid" 422 fires and the dialog's EPSG
# override (built for .prj-less shapefiles) becomes required.
_CSV_X_LATLON = ("lon", "long", "longitude", "lng")
_CSV_Y_LATLON = ("lat", "latitude")
_CSV_X_PROJ = ("x", "easting", "point_x", "xcoord", "x_coord")
_CSV_Y_PROJ = ("y", "northing", "point_y", "ycoord", "y_coord")
_CSV_SNIFF_ROWS = 200      # type-sniff sample size (header excluded)
_CSV_DIALECT_PROBE = 8192  # bytes handed to csv.Sniffer for delimiter detection
# The integer spelling a spreadsheet column actually means — ASCII digits, optional sign, nothing
# else. Deliberately NARROWER than int(), which also accepts underscores ('1_000') and non-ASCII
# digit systems. See _csv_num.
_CSV_INT_RE = re.compile(r"^[+-]?[0-9]+$")
# Likewise for decimals. float() additionally accepts 'nan', 'inf' and 'Infinity' BY NAME, and
# overflows '1e999' to Infinity — none of which a CSV cell means as a number, and all of which
# PostGIS will happily store as a coordinate.
_CSV_FLOAT_RE = re.compile(r"^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")

_ZIP_MAX_ENTRIES = 64          # a shapefile set is a handful of files; a county GPKG is one
_SNIFF_GEOJSON_MAX = 512 * 1024 * 1024  # json.load sniff guard; stage-6 ijson streaming lifts this
# KML/KMZ. v1 parses the whole document with the stdlib tree
# parser, like the GeoJSON v1 path — at a tighter guard, since an XML DOM is several times the
# file. Google Earth KMZs are typically a few MiB; a county's worth of placemarks is tens.
_KML_MAX = 256 * 1024 * 1024
# The document prolog is scanned for a DTD so the common case gets a plain "refused" instead of a
# parser error: a KML never needs one. This scan is the BELT, not the fence — it reads only the
# first 64 KiB and matches ASCII bytes, so a long leading comment or a UTF-16 document gets past
# it (security review 2026-09-17). The fence is the parser: stdlib ElementTree resolves NO external
# entities and fetches NO external DTD (an `&xxe;` is an "undefined entity" ParseError → 422), and
# expat ≥ 2.4 caps internal-entity amplification. test_import_kml pins both bypasses as still
# refused. Never swap in a parser that resolves entities (lxml, a custom `entity` map) without
# re-reading this.
_KML_PROLOG_SCAN = 64 * 1024
_KML_DOCTYPE_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
# Nesting cap for Folder/Document and MultiGeometry recursion: real files nest a handful deep; a
# thousand-deep file is either broken or hostile, and Python's recursion limit would turn it into a
# 500 instead of the sniffers' honest 422.
_KML_MAX_DEPTH = 64
# KML SimpleField types (OGC KML 2.2 §9.2) → house draft types. Anything else is text.
_KML_SCHEMA_TYPES = {
    "int": ("number", "long_integer"), "uint": ("number", "long_integer"),
    "short": ("number", "long_integer"), "ushort": ("number", "long_integer"),
    "float": ("number", "double"), "double": ("number", "double"),
    "bool": ("boolean", None), "string": ("text", None),
}
# Feature-like elements a KML carries that are NOT importable geometry. They are surfaced as
# failed rows with a reason (visible in the job's error sample) rather than dropped silently —
# and a NetworkLink's href is NEVER fetched (kickoff F7).
_KML_SKIPPED = frozenset({"GroundOverlay", "ScreenOverlay", "PhotoOverlay", "NetworkLink"})

# SHP shapeType -> Talus geometry family (pyshp constants; %10 folds Z/M variants)
_SHP_FAMILY = {1: "point", 8: "point", 3: "line", 5: "polygon"}
_GPKG_FAMILY = {
    "POINT": "point", "MULTIPOINT": "point",
    "LINESTRING": "line", "MULTILINESTRING": "line",
    "POLYGON": "polygon", "MULTIPOLYGON": "polygon",
}
_GEOJSON_FAMILY = {
    "Point": "point", "MultiPoint": "point",
    "LineString": "line", "MultiLineString": "line",
    "Polygon": "polygon", "MultiPolygon": "polygon",
}


# --- staging ------------------------------------------------------------------------------------

def staging_root() -> str:
    root = settings.import_staging_dir or os.path.join(tempfile.gettempdir(), "talus-import-jobs")
    # 0700: the staging root is a PREDICTABLE shared path, so give it the same owner-only
    # posture the rasters precedent gets from tempfile.mkstemp (security-review note,
    # 2026-08-21). Per-job dirs below it are uuid4-named; this closes the co-resident-user
    # read on the parent. chmod separately — makedirs' mode is umask-masked and a no-op
    # when the directory already exists.
    os.makedirs(root, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:  # a mount that refuses chmod must not break imports
        pass
    return root


def job_dir(job_id: str) -> str:
    """The jail. Derived from the job id ONLY (a uuid we minted) — never client input."""
    return os.path.join(staging_root(), str(job_id))


def reap_staging(job_id: str) -> None:
    """Terminal-state cleanup — ALWAYS runs (completed/failed/canceled). Idempotent."""
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


# --- archive validation (zip-slip + bomb budget) ------------------------------------------------

def _reject(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _sqlite_ident(name: str) -> str:
    """Quote a SQLite identifier read OUT OF THE UPLOADED FILE (a GeoPackage's own table
    name), doubling embedded quotes — the same treatment `layer_fields._geopackage_to_drafts`
    already gives it. The blast radius is the attacker's own read-only in-file database, but
    the two GPKG paths must not disagree on something like this (security-review note)."""
    return '"' + str(name).replace('"', '""') + '"'


def validate_and_extract_zip(zip_path: str, dest_dir: str) -> list[str]:
    """Extract with every entry validated BEFORE a byte lands: no absolute paths, no `..`
    segments, no symlinks, regular files only, bounded entry count, and a running
    decompressed-size budget enforced during the copy (never trusted from headers alone).
    Returns extracted basenames. 422 names the offending entry; 413 on budget."""
    budget = settings.import_max_unpacked_mb * 1024 * 1024
    extracted: list[str] = []
    total = 0
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise _reject("not a valid zip archive")
    with zf:
        infos = zf.infolist()
        if len(infos) > _ZIP_MAX_ENTRIES:
            raise _reject(f"archive has too many entries (max {_ZIP_MAX_ENTRIES})")
        for info in infos:
            name = info.filename
            if name.endswith("/"):
                continue  # directory entries carry nothing; we flatten to basenames anyway
            parts = name.replace("\\", "/").split("/")
            if name.startswith(("/", "\\")) or ".." in parts:
                raise _reject(f"archive entry refuses extraction (path traversal): {name}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and stat.S_ISLNK(mode):
                raise _reject(f"archive entry refuses extraction (symlink): {name}")
            base = parts[-1]
            if not base:
                raise _reject(f"archive entry has no usable name: {name}")
            out_path = os.path.join(dest_dir, base)
            # flattening collision = ambiguous source; refuse rather than guess
            if os.path.exists(out_path):
                raise _reject(f"archive contains duplicate member name: {base}")
            with zf.open(info) as src, open(out_path, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > budget:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=(
                                f"archive decompresses past the {settings.import_max_unpacked_mb}"
                                " MiB budget"
                            ),
                        )
                    dst.write(chunk)
            extracted.append(base)
    return extracted


# --- KML / KMZ --------------------------------------------------------------------------------

def _kml_tag(el) -> str:
    """Local name of an element: KML is namespaced (`{http://www.opengis.net/kml/2.2}Placemark`,
    sometimes the 2.1 or Google namespaces, sometimes none at all from older exporters)."""
    t = el.tag
    return t.rsplit("}", 1)[-1] if isinstance(t, str) else ""


def _kml_child(el, name: str):
    for c in el:
        if _kml_tag(c) == name:
            return c
    return None


def _kml_text(el, name: str) -> str | None:
    c = _kml_child(el, name)
    if c is None or c.text is None:
        return None
    t = c.text.strip()
    return t if t else None


def kml_document_path(path: str, jail: str) -> str:
    """The .kml document to parse: `path` itself, or — when the upload is a KMZ (zip magic, never
    the extension) — the archive's `doc.kml` or first `.kml` member, extracted through the SAME
    validator as a Shapefile zip (traversal / symlink / entry-count / decompression-budget fences).
    Bundled images and models are extracted alongside and ignored.

    The archive is unpacked ONCE, into `<jail>/kmz/`, and any later call reuses it: the router's
    sniff and the worker's iterator both go through here, and the first live KMZ job (2026-09-17)
    died on the validator's duplicate-member guard when the worker unpacked a second time into the
    same directory. The subdirectory also keeps a member that happens to be named `upload.kml`
    from colliding with the staged upload itself. Mirrors the Shapefile path, where the router
    extracts and `_iter_shp` reads the jail."""
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] != b"PK":
        return path
    unpack = os.path.join(jail, "kmz")
    if os.path.isdir(unpack):
        members = os.listdir(unpack)
    else:
        os.makedirs(unpack)
        members = validate_and_extract_zip(path, unpack)
    kmls = [m for m in members if m.lower().endswith(".kml")]
    if not kmls:
        raise _reject("KMZ archive contains no .kml document")
    doc = next((m for m in kmls if m.lower() == "doc.kml"), sorted(kmls)[0])
    return os.path.join(unpack, doc)


def _kml_parse(doc_path: str):
    """Parse the document with the stdlib tree parser after the size guard and the DTD pre-scan
    (see the note at `_KML_DOCTYPE_RE`: the parser's own no-external-entity default is the fence;
    the pre-scan only makes the common case legible). Returns the root element; 422 with a plain
    reason on anything else."""
    import xml.etree.ElementTree as ET

    if os.path.getsize(doc_path) > _KML_MAX:
        raise _reject("KML document over the 256 MiB server-parse guard — convert to GeoPackage for now")
    with open(doc_path, "rb") as fh:
        prolog = fh.read(_KML_PROLOG_SCAN)
    if _KML_DOCTYPE_RE.search(prolog):
        raise _reject("KML document declares a DTD or entities — refused (a KML never needs one)")
    try:
        return ET.parse(doc_path).getroot()
    except ET.ParseError as e:
        raise _reject(f"not well-formed KML/XML ({e})")
    except (OSError, ValueError):
        raise _reject("KML document could not be read")


def _kml_coords(text: str | None) -> list[list[float]]:
    """`lon,lat[,alt] lon,lat[,alt] …` → [[lon, lat], …]. Altitude is dropped (2D maps, full stop);
    a tuple that is not two finite numbers makes the whole geometry invalid."""
    out: list[list[float]] = []
    for tup in (text or "").split():
        parts = tup.split(",")
        if len(parts) < 2:
            raise ValueError("coordinate tuple needs lon,lat")
        x, y = float(parts[0]), float(parts[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("non-finite coordinate")
        out.append([x, y])
    return out


def _kml_ring(el) -> list[list[float]]:
    lr = _kml_child(el, "LinearRing")
    ring = _kml_coords(_kml_text(lr, "coordinates") if lr is not None else None)
    if len(ring) < 4:
        raise ValueError("polygon ring needs at least four positions")
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))  # KML rings are usually closed; GeoJSON requires it
    return ring


def _kml_geometry(el, depth: int = 0) -> tuple[str, dict[str, Any]]:
    """One KML geometry element → (family, GeoJSON geometry). MultiGeometry flattens to a
    Multi* of ONE family; a Placemark mixing families is an error row (the GeoJSON rule: one
    family per layer, and a feature is one geometry). Unsupported elements (Model, gx:Track,
    gx:MultiTrack) raise; so does MultiGeometry nested past _KML_MAX_DEPTH."""
    if depth > _KML_MAX_DEPTH:
        raise ValueError(f"geometry nested deeper than {_KML_MAX_DEPTH} levels")
    tag = _kml_tag(el)
    if tag == "Point":
        pts = _kml_coords(_kml_text(el, "coordinates"))
        if len(pts) != 1:
            raise ValueError("Point needs exactly one position")
        return "point", {"type": "Point", "coordinates": pts[0]}
    if tag == "LineString":
        pts = _kml_coords(_kml_text(el, "coordinates"))
        if len(pts) < 2:
            raise ValueError("LineString needs at least two positions")
        return "line", {"type": "LineString", "coordinates": pts}
    if tag == "Polygon":
        outer = _kml_child(el, "outerBoundaryIs")
        if outer is None:
            raise ValueError("Polygon has no outerBoundaryIs")
        rings = [_kml_ring(outer)]
        for c in el:
            if _kml_tag(c) == "innerBoundaryIs":
                rings.append(_kml_ring(c))
        return "polygon", {"type": "Polygon", "coordinates": rings}
    if tag == "MultiGeometry":
        parts = [_kml_geometry(c, depth + 1) for c in el if _kml_tag(c) in ("Point", "LineString", "Polygon", "MultiGeometry")]
        if not parts:
            raise ValueError("MultiGeometry is empty")
        families = {f for f, _g in parts}
        if len(families) != 1:
            raise ValueError("Placemark mixes geometry families (" + ", ".join(sorted(families)) + ")")
        family = parts[0][0]
        coords: list[Any] = []
        for _f, g in parts:
            if g["type"].startswith("Multi"):
                coords.extend(g["coordinates"])
            else:
                coords.append(g["coordinates"])
        mtype = {"point": "MultiPoint", "line": "MultiLineString", "polygon": "MultiPolygon"}[family]
        return family, {"type": mtype, "coordinates": coords}
    raise ValueError(f"unsupported KML geometry ({tag or 'unknown'})")


def _kml_placemark_geometry(pm):
    for c in pm:
        if _kml_tag(c) in ("Point", "LineString", "Polygon", "MultiGeometry", "Model", "Track", "MultiTrack"):
            return c
    return None


def _kml_schema_types(root) -> dict[str, tuple[str, str | None]]:
    """`<Schema><SimpleField type=… name=…>` anywhere in the document → {name: (data_type, subtype)}."""
    out: dict[str, tuple[str, str | None]] = {}
    for sch in root.iter():
        if _kml_tag(sch) != "Schema":
            continue
        for f in sch:
            if _kml_tag(f) == "SimpleField" and f.get("name"):
                out[f.get("name")] = _KML_SCHEMA_TYPES.get((f.get("type") or "string").lower(), ("text", None))
    return out


def _kml_props(pm, folder: str | None) -> dict[str, Any]:
    """name, description (raw text — Google Earth stuffs HTML in it; stored as text, never
    rendered as HTML), the folder path, then ExtendedData: `Data[@name]/value` and
    `SchemaData/SimpleData[@name]`. Untyped here; _iter_kml coerces by Schema type."""
    props: dict[str, Any] = {}
    name = _kml_text(pm, "name")
    if name is not None:
        props["name"] = name
    desc = _kml_text(pm, "description")
    if desc is not None:
        props["description"] = desc
    if folder:
        props["kml_folder"] = folder
    ext = _kml_child(pm, "ExtendedData")
    if ext is not None:
        for c in ext:
            t = _kml_tag(c)
            if t == "Data" and c.get("name"):
                props[c.get("name")] = _kml_text(c, "value")
            elif t == "SchemaData":
                for sd in c:
                    if _kml_tag(sd) == "SimpleData" and sd.get("name"):
                        props[sd.get("name")] = (sd.text or "").strip() or None
    return props


def _kml_walk(el, folder: list[str], depth: int = 0):
    """Depth-first over Document/Folder, yielding (placemark_or_skipped_element, folder_path).
    Folder names join with '/'; a nameless Folder contributes nothing to the path. Nesting past
    _KML_MAX_DEPTH is refused with the sniffers' 422 (the worker's broad except turns the same
    exception into a failed job with this reason)."""
    if depth > _KML_MAX_DEPTH:
        raise _reject(f"KML nests folders deeper than {_KML_MAX_DEPTH} levels")
    for c in el:
        t = _kml_tag(c)
        if t in ("Document", "Folder"):
            n = _kml_text(c, "name") if t == "Folder" else None
            if n:
                folder.append(n)
            yield from _kml_walk(c, folder, depth + 1)
            if n:
                folder.pop()
        elif t == "Placemark" or t in _KML_SKIPPED:
            yield c, "/".join(folder)


def sniff_kml(path: str, jail: str) -> tuple[list[dict[str, Any]], str, int | None]:
    """(drafts, family, 4326) from the FIRST Placemark that carries a supported geometry. KML is
    WGS-84 by definition (OGC KML 2.2 §6.2), so there is no CRS to detect and no picker to offer.
    Drafts: name / description / kml_folder as text, then every field the document's `<Schema>`
    declares (typed from it), then any extra ExtendedData name the first placemark carries (text).
    The Schema comes first because real writers (GDAL's KML driver, ArcGIS Layer-to-KML) declare
    the full field list once and then omit empty values per placemark — a real county wetland
    export carried 6 of its 10 Schema fields on any given row. Drafting from the
    first placemark alone would silently drop a column every later row happens to have."""
    root = _kml_parse(kml_document_path(path, jail))
    types = _kml_schema_types(root)
    skipped = 0
    no_geom = 0
    with_address = 0
    for el, folder in _kml_walk(root, []):
        if _kml_tag(el) != "Placemark":
            skipped += 1
            continue
        g = _kml_placemark_geometry(el)
        if g is None:
            # Google My Maps exports a layer that was built from a spreadsheet of addresses with an
            # <address> element and NO coordinates — Google geocodes it at display time (real file,
            # 2026-09-17: 371 placemarks, 197 with <address>, 0 with <Point>). Count them so the
            # refusal below can say what the file is and where such rows go instead.
            no_geom += 1
            if _kml_child(el, "address") is not None:
                with_address += 1
            continue
        try:
            family, _geom = _kml_geometry(g)
        except ValueError:
            continue
        taken: set[str] = set()
        drafts = [_draft("name", "text", taken), _draft("description", "text", taken)]
        if folder:
            drafts.append(_draft("kml_folder", "text", taken))
        seen = {"name", "description", "kml_folder"}
        for k, (dt, sub) in types.items():
            if k not in seen:
                drafts.append(_draft(k, dt, taken, sub))
                seen.add(k)
        for k in _kml_props(el, folder):
            if k not in seen:
                drafts.append(_draft(k, "text", taken))
                seen.add(k)
        return drafts, family, 4326
    if no_geom:
        hint = (
            f" — {with_address} of them carry a Google Maps <address> instead of coordinates (a Google My "
            "Maps layer built from addresses is geocoded at display time, not in the file). Use Catalog → "
            "Batch geocode addresses and choose this file to make a point layer"
            if with_address else
            " — none of them carry coordinates; add coordinates before exporting, or use Catalog → "
            "Batch geocode addresses with a column that holds the address"
        )
        raise _reject(f"KML has {no_geom} Placemarks but none with geometry{hint}")
    if skipped:
        raise _reject(f"KML has no Placemarks with geometry ({skipped} overlays / network links are not importable)")
    raise _reject("KML has no Placemarks with geometry")


def _kml_coerce(props: dict[str, Any], types: dict[str, tuple[str, str | None]]) -> dict[str, Any]:
    """Schema-typed SimpleData arrives as text; hand the E4 validator the declared type. A value
    that does not parse is left as text so validate_props reports it rather than this loop guessing."""
    for k, (dt, _sub) in types.items():
        v = props.get(k)
        if v is None or not isinstance(v, str):
            continue
        try:
            if dt == "number":
                props[k] = float(v) if ("." in v or "e" in v.lower()) else int(v)
            elif dt == "boolean":
                if v.strip().lower() in ("1", "true"):
                    props[k] = True
                elif v.strip().lower() in ("0", "false"):
                    props[k] = False
        except ValueError:
            pass
    return props


def _iter_kml(path: str, jail: str):
    """Yields the E4 raw-feature shape (kind='geojson'). Every Placemark is a row; one without a
    usable geometry, or of a family the layer cannot hold, is an error row with the reason; an
    overlay or NetworkLink is an error row too, so the job report says what a Google Earth file
    carried that Talus does not import (and nothing is ever fetched)."""
    root = _kml_parse(kml_document_path(path, jail))
    types = _kml_schema_types(root)
    for el, folder in _kml_walk(root, []):
        tag = _kml_tag(el)
        if tag in _KML_SKIPPED:
            yield {"family": None, "geom": None, "props": {},
                   "error": f"{tag} not imported (overlays and network links are skipped; nothing was fetched)"}
            continue
        props = _kml_coerce(_kml_props(el, folder), types)
        g = _kml_placemark_geometry(el)
        if g is None:
            yield {"family": None, "geom": None, "props": props, "error": "Placemark has no geometry"}
            continue
        try:
            family, geom = _kml_geometry(g)
        except ValueError as e:
            yield {"family": None, "geom": None, "props": props, "error": f"missing or unsupported geometry ({e})"}
            continue
        yield {"family": family, "geom": geom, "props": props, "error": None}


def kml_rows(path: str, jail: str) -> tuple[list[str], list[dict[str, str]], dict[str, int]]:
    """G3b (decision-kml-address-geocode.md): every Placemark as a TEXT row for batch geocoding —
    the `parse_csv` shape, `(headers, rows-as-dicts)`, plus counts. The placemark's `<address>`
    becomes an `address` column, first in the headers, and is omitted altogether when no placemark
    carries one; name / description / kml_folder / every ExtendedData value ride along as text
    (the batch output types every source column as text by design). Rows that already carry
    geometry are included and counted — the caller decides whether to say "import those instead".
    Overlays and NetworkLinks are skipped and counted; nothing is fetched. Values are never logged."""
    root = _kml_parse(kml_document_path(path, jail))
    stats = {"placemarks": 0, "withAddress": 0, "withGeometry": 0, "skipped": 0}
    headers: list[str] = ["address"]
    seen = {"address"}
    rows: list[dict[str, str]] = []
    for el, folder in _kml_walk(root, []):
        if _kml_tag(el) in _KML_SKIPPED:
            stats["skipped"] += 1
            continue
        stats["placemarks"] += 1
        addr = _kml_text(el, "address") or ""
        if addr:
            stats["withAddress"] += 1
        if _kml_placemark_geometry(el) is not None:
            stats["withGeometry"] += 1
        row: dict[str, str] = {"address": addr}
        for k, v in _kml_props(el, folder).items():
            row[k] = "" if v is None else str(v)
            if k not in seen:
                headers.append(k)
                seen.add(k)
        rows.append(row)
    if not rows:
        raise _reject("KML has no Placemarks")
    if stats["withAddress"] == 0:
        headers.remove("address")
    return headers, [{h: r.get(h, "") for h in headers} for r in rows], stats


# --- schema sniff (cheap, endpoint-time; feeds target=new provisioning) -------------------------

def _draft(name: str, data_type: str, taken: set[str], subtype: str | None = None) -> dict[str, Any]:
    """One format-neutral draft in the HOUSE shape `_run_field_import` consumes — snake_case keys,
    `_sanitize_key` for the field key (the ONE source-column derivation, which also applies §3.1's
    reserved-name rename: globalid -> globalid_src) and `_renamed_from` so the rename stays legible.

    ★ This shape is load-bearing twice over: the shared core reads `field_key`/`data_type` (camelCase
    silently yields "unknown data type ''" for EVERY draft — the 2026-08-21 attribute-loss bug), and
    the worker matches source columns to fields with the same `_sanitize_key`, so any private slug
    here would silently drop props. `taken` is unused now (duplicates are the core's own 'duplicate'
    verdict, last-wins at insert — the documented E4 behavior) and kept for signature stability.
    """
    from .routers.layer_fields import _renamed_from, _sanitize_key

    return {
        "field_key": _sanitize_key(name),
        "_renamed_from": _renamed_from(name),
        "label": name,
        "data_type": data_type,
        "required": False,
        "default_value": None,
        "default_kind": "constant",
        "constraints": None,
        "numeric_subtype": subtype if data_type == "number" else None,
    }


def sniff_shp(dir_path: str) -> tuple[list[dict[str, Any]], str, int | None, str]:
    """(field drafts, geometry family, detected srid, shp basename) from an extracted set."""
    import shapefile  # pyshp — already a declared dependency (E4)

    shp = next((f for f in sorted(os.listdir(dir_path)) if f.lower().endswith(".shp")), None)
    if shp is None:
        raise _reject("archive contains no .shp member")
    try:
        reader = shapefile.Reader(os.path.join(dir_path, shp))
        family = _SHP_FAMILY.get(reader.shapeType % 10)
        fields = reader.fields[1:]  # [0] is the DeletionFlag
    except HTTPException:
        raise
    except Exception:
        raise _reject("could not read the Shapefile")
    if family is None:
        raise _reject(f"unsupported shapefile geometry type ({reader.shapeType})")
    taken: set[str] = set()
    drafts = []
    for fname, ftype, _sz, fdec in fields:
        if ftype in ("N", "F"):
            drafts.append(_draft(fname, "number", taken,
                                 "double" if (ftype == "F" or fdec) else "long_integer"))
        elif ftype == "L":
            drafts.append(_draft(fname, "boolean", taken))
        elif ftype == "D":
            drafts.append(_draft(fname, "date", taken))
        else:
            drafts.append(_draft(fname, "text", taken))
    detected: int | None = None
    prj = os.path.join(dir_path, shp[:-4] + ".prj")
    if os.path.exists(prj):
        try:
            with open(prj, encoding="utf-8", errors="replace") as fh:
                detected = srid_from_prj(fh.read())
        except OSError:
            detected = None
    return drafts, family, detected, shp


def sniff_gpkg(path: str) -> tuple[list[dict[str, Any]], str, int | None, str]:
    """(drafts, family, srid, table name) from a GeoPackage — stdlib sqlite3, read-only."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            gpkg_guard.guard(con, gpkg_guard.PREVIEW_BUDGET_OPS)  # read-only, untrusting, budgeted (K1)
            gpkg_guard.require_plain_table(con, "gpkg_geometry_columns")
            row = con.execute(
                "SELECT table_name, geometry_type_name, srs_id, column_name "
                "FROM gpkg_geometry_columns LIMIT 1"
            ).fetchone()
            if row is None:
                raise _reject("GeoPackage declares no feature layer (gpkg_geometry_columns empty)")
            table, gtype, srs_id, geom_col = row
            gpkg_guard.require_plain_table(con, table)  # a view / virtual table / generated column is refused
            cols = con.execute(f"PRAGMA table_info({_sqlite_ident(table)})").fetchall()
        finally:
            con.close()
    except HTTPException:
        raise
    except gpkg_guard.UnsafeGeoPackage as exc:
        raise _reject(str(exc))
    except sqlite3.Error as exc:
        if gpkg_guard.is_budget_stop(exc):
            raise _reject(gpkg_guard.TOO_COSTLY)
        raise _reject("not a readable GeoPackage")
    family = _GPKG_FAMILY.get((gtype or "").upper())
    if family is None:
        raise _reject(f"unsupported GeoPackage geometry type ({gtype})")
    taken: set[str] = set()
    drafts = []
    for _cid, name, ctype, _nn, _dflt, pk in cols:
        if pk or name == geom_col:
            continue
        ct = (ctype or "").upper()
        if ct.startswith("INT"):
            drafts.append(_draft(name, "number", taken, "long_integer"))
        elif ct in ("REAL", "FLOAT", "DOUBLE"):
            drafts.append(_draft(name, "number", taken, "double"))
        elif ct == "DATE":
            drafts.append(_draft(name, "date", taken))
        elif ct.startswith("BOOL"):
            drafts.append(_draft(name, "boolean", taken))
        else:
            drafts.append(_draft(name, "text", taken))
    srid = int(srs_id) if srs_id and int(srs_id) > 0 else None
    return drafts, family, srid, table


def sniff_geojson(path: str) -> tuple[list[dict[str, Any]], str, int | None]:
    """(drafts, family, 4326). Geometry family from the FIRST feature; field types from a sample
    of every column's values, through the same sampler the data-table import uses. v1 bounds the
    sniff with a whole-file json.load behind a size guard; the stage-6 ijson streaming parser
    lifts the guard (the worker will share it). RFC 7946: GeoJSON is CRS84/4326 (the E4 P2 rule).

    ★ Typing from the first feature alone refused whole files: `_iter_geojson` passes values
    through untouched, so a column the first feature typed wrongly had every later row refused by
    the field validator — a null first (typed text, numbers refused), a decimal after a whole
    number, or a whole number past 32 bits (epoch milliseconds, the way an ArcGIS service writes
    a date in GeoJSON). The sampler skips nulls, recognises YYYY-MM-DD strings as dates, and
    widens a number column to double when any value needs it."""
    if os.path.getsize(path) > _SNIFF_GEOJSON_MAX:
        raise _reject(
            "GeoJSON over the current 512 MiB server-parse guard — convert to GeoPackage or "
            "zipped Shapefile for now (streaming GeoJSON lands with this slice's parser stage)"
        )
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError):
        raise _reject("not valid GeoJSON (the document itself failed to parse server-side)")
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list) or not feats:
        raise _reject("GeoJSON has no features array (expected a FeatureCollection)")
    first = feats[0] if isinstance(feats[0], dict) else {}
    gtype = (first.get("geometry") or {}).get("type")
    family = _GEOJSON_FAMILY.get(gtype)
    if family is None:
        raise _reject(f"unsupported GeoJSON geometry type ({gtype})")
    from .routers.layer_fields import _MAX_IMPORT_FIELDS, _new_sniff, _sniff_json_value, _sniffed_type

    acc: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for f in feats:
        props = f.get("properties") if isinstance(f, dict) else None
        if not isinstance(props, dict):
            continue
        for name, v in props.items():
            a = acc.get(name)
            if a is None:
                # One past the cap is enough for the shared core to refuse the file by name
                # ("too many fields"); a document with a new key per feature must not grow this.
                if not name.strip() or len(acc) > _MAX_IMPORT_FIELDS:
                    continue
                a = acc[name] = _new_sniff()
                order.append(name)
            _sniff_json_value(a, v)
    taken: set[str] = set()
    drafts = []
    for name in order:
        data_type, subtype = _sniffed_type(acc[name])
        drafts.append(_draft(name, data_type, taken, subtype))
    return drafts, family, 4326


def _csv_open(path: str):
    """Open a staged CSV the one house way: utf-8-sig (Excel writes a BOM), newline='' per the
    csv module's contract, csv.Sniffer for the delimiter over a bounded probe with a comma
    fallback (Sniffer throws on single-column files, which are legal here right up until the
    coordinate check rejects them with a better message)."""
    import csv

    fh = open(path, encoding="utf-8-sig", errors="replace", newline="")
    try:
        probe = fh.read(_CSV_DIALECT_PROBE)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(probe, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return fh, csv.reader(fh, dialect)
    except BaseException:
        fh.close()
        raise


def _csv_xy(headers: list[str]) -> tuple[int, int, int | None]:
    """(x index, y index, detected srid) from the header row, or a 422 naming what was seen.
    Exactly one candidate per axis; both axes must come from the SAME family (a 'lon' paired
    with a bare 'y' is a mislabeled file, not a guess to make)."""
    lowered = [h.strip().lower() for h in headers]

    def _one(cands: tuple[str, ...]) -> int | None:
        hits = [i for i, h in enumerate(lowered) if h in cands]
        if len(hits) > 1:
            raise _reject(
                f"multiple coordinate-candidate columns ({', '.join(headers[i] for i in hits)}) — "
                "rename so exactly one X and one Y column remain"
            )
        return hits[0] if hits else None

    xi_ll, yi_ll = _one(_CSV_X_LATLON), _one(_CSV_Y_LATLON)
    if xi_ll is not None and yi_ll is not None:
        return xi_ll, yi_ll, 4326
    xi_pr, yi_pr = _one(_CSV_X_PROJ), _one(_CSV_Y_PROJ)
    if xi_pr is not None and yi_pr is not None:
        return xi_pr, yi_pr, None  # projected family: the caller must declare the EPSG
    raise _reject(
        "no coordinate columns found — expected a lon/lat pair "
        f"({'/'.join(_CSV_X_LATLON)} + {'/'.join(_CSV_Y_LATLON)}) or a projected x/y pair "
        f"({'/'.join(_CSV_X_PROJ)} + {'/'.join(_CSV_Y_PROJ)}); "
        f"this file's header is: {', '.join(headers[:20]) or '(empty)'}"
    )


def _csv_num(v: str):
    """Best-effort scalar coercion for a CSV cell: int → float → stripped string; '' → None.
    The same rule the type sniff samples with, so a column the sniff called numeric receives
    numbers at insert time (the E4 core's validators expect the drafted type).

    ★ LEADING ZEROS ARE AN IDENTIFIER, NOT A NUMBER. '01234' is a ZIP, a FIPS code, a parcel PIN
    or a well ID — the kind of column municipal data is full of. int() renders it 1234, and since
    the staged file is reaped on completion and the job reports success, the original is gone.
    Such a value stays a string, so the column types as text and it round-trips verbatim — the
    lossless direction. ('0' is still 0; '0.5' is still 0.5.)

    ★ NON-FINITE VALUES ARE NOT NUMBERS EITHER. 'nan', 'inf', 'Infinity' and '1e999' all reach
    float() as NaN/±Infinity. As a COORDINATE those pass an isinstance() guard and PostGIS stores
    them without complaint — ST_IsValid(POINT(NaN NaN)) is even true — and ST_Extent over a set
    containing ONE of them returns BOX(NaN NaN,NaN NaN), so a single bad cell breaks zoom-to-layer
    and the extent picker for every clean feature in the layer. They stay text; _csv_finite() is
    the wall on the coordinate path."""
    s = v.strip()
    if s == "":
        return None
    if _CSV_INT_RE.match(s):
        digits = s.lstrip("+-")
        if len(digits) > 1 and digits[0] == "0":
            return s  # leading zero ⇒ identifier, keep it verbatim
        try:
            return int(s)
        except ValueError:
            return s  # past CPython's 4300-digit int() ceiling: text, never a lossy float
    if _CSV_FLOAT_RE.match(s):
        f = float(s)
        return f if math.isfinite(f) else s  # '1e999' overflows to inf — keep the text
    return s


def _csv_finite(n: Any) -> bool:
    """A coordinate must be a real number — not NaN, not ±Infinity (see _iter_csv)."""
    return isinstance(n, (int, float)) and math.isfinite(n)


def _csv_scan(path: str) -> tuple[list[str], int, int, int | None, list[int], dict[int, str]]:
    """ONE header + bounded-sample pass shared by sniff_csv AND _iter_csv:
    (headers, x idx, y idx, detected srid, attribute idxs, per-column kind 'int'|'float'|'text').

    ★ The sniff and the iterator MUST consume the same per-column decision. The first cut
    coerced per-VALUE in the iterator while typing per-COLUMN in the sniff — a column the
    sample typed as text (e.g. trunk_height carrying '10-15') then received numbers from
    later numeric-looking cells, and the E4 validator refused every such row (2,977 refusals
    on the first real file, 2026-09-03). Per-column agreement is the contract."""
    fh, reader = _csv_open(path)
    try:
        try:
            headers = next(reader)
        except StopIteration:
            raise _reject("the CSV is empty (no header row)")
        if not any(h.strip() for h in headers):
            raise _reject("the CSV's first row is blank — a header row is required")
        xi, yi, detected = _csv_xy(headers)
        attr_idx = [i for i, h in enumerate(headers) if i not in (xi, yi) and h.strip()]
        kinds: dict[int, str] = {}  # absent = no non-empty sample yet (lands as text — lossless)
        sampled = 0
        for row in reader:
            if sampled >= _CSV_SNIFF_ROWS:
                break
            sampled += 1
            for i in attr_idx:
                if i >= len(row):
                    continue
                v = _csv_num(row[i])
                if v is None:
                    continue
                seen = "int" if isinstance(v, int) else "float" if isinstance(v, float) else "text"
                prev = kinds.get(i)
                if prev is None:
                    kinds[i] = seen
                elif {prev, seen} == {"int", "float"}:
                    kinds[i] = "float"
                elif prev != seen:
                    kinds[i] = "text"
    finally:
        fh.close()
    return headers, xi, yi, detected, attr_idx, kinds


def sniff_csv(path: str) -> tuple[list[dict[str, Any]], str, int | None]:
    """(drafts, 'point', detected srid) from the shared scan. Points
    only; coordinate columns become geometry, not fields; remaining columns type to
    long_integer / double / text (no date/boolean guessing)."""
    headers, _xi, _yi, detected, attr_idx, kinds = _csv_scan(path)
    taken: set[str] = set()
    drafts = []
    for i in attr_idx:
        kind = kinds.get(i, "text")
        if kind == "int":
            drafts.append(_draft(headers[i], "number", taken, "long_integer"))
        elif kind == "float":
            drafts.append(_draft(headers[i], "number", taken, "double"))
        else:
            drafts.append(_draft(headers[i], "text", taken))
    return drafts, "point", detected


# --- job CRUD -----------------------------------------------------------------------------------

_JOB_COLS = (
    "id, status, format, original_name, staged_bytes, target_layer_id, created_new, "
    "source_srid, total_rows, processed_rows, inserted_rows, failed_rows, error_sample, "
    "error, created_by, created_at, started_at, finished_at, updated_at"
)


def _job_dict(row: tuple) -> dict[str, Any]:
    keys = [c.strip() for c in _JOB_COLS.split(",")]
    d = dict(zip(keys, row))
    d["id"] = str(d["id"])
    d["created_by"] = str(d["created_by"])
    for k in ("created_at", "started_at", "finished_at", "updated_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


async def create_job(
    conn: AsyncConnection, *, job_id: UUID, fmt: str, original_name: str, staged_bytes: int,
    target_layer_id: str, created_new: bool, source_srid: int | None, actor: UUID,
) -> dict[str, Any]:
    """job_id is minted by the ROUTER before staging (the jail dir needs it pre-row) and
    supplied explicitly — the staging path and the row key are the same uuid by design."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"INSERT INTO platform.import_jobs "
            f"(id, format, original_name, staged_bytes, target_layer_id, created_new, source_srid, created_by) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_JOB_COLS}",
            (job_id, fmt, original_name, staged_bytes, target_layer_id, created_new, source_srid, actor),
        )
        row = await cur.fetchone()
    await conn.commit()
    return _job_dict(row)


async def get_job_owned(conn: AsyncConnection, job_id: str, actor: UUID) -> dict[str, Any] | None:
    """Owner-scoped read: someone else's job is a 404 upstream (existence not disclosed)."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_JOB_COLS} FROM platform.import_jobs WHERE id = %s AND created_by = %s",
            (job_id, actor),
        )
        row = await cur.fetchone()
    return _job_dict(row) if row else None


# --- the worker half (import_batch_worker.py drives this; decision doc §4/§5) -------------------
#
# Batches are the atomicity unit: each batch runs ONE transaction — audit context, the E4
# INSERT (same _ingest_geom_expr/props machinery as the sync door), and the counter UPDATE —
# so a crash resumes by skipping processed_rows source rows with nothing double-counted.
# Cancel is honored at batch boundaries. Terminal states ALWAYS reap the staging jail.

_BATCH = 2500


async def claim_next_job(conn: AsyncConnection) -> dict[str, Any] | None:
    """Oldest live job, FIFO. A 'running' row can only be an orphan (single worker under the
    advisory lock), so reclaiming it IS the crash-resume path."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE platform.import_jobs SET status = 'running', "
            f"started_at = COALESCE(started_at, now()), updated_at = now() "
            f"WHERE id = (SELECT id FROM platform.import_jobs "
            f"            WHERE status IN ('pending', 'running') "
            f"            ORDER BY created_at LIMIT 1) RETURNING {_JOB_COLS}",
        )
        row = await cur.fetchone()
    await conn.commit()
    return _job_dict(row) if row else None


def _iter_geojson(path: str):
    """v1: whole-file json.load behind the sniff guard (stage-6 ijson lifts it). Yields the
    E4 raw-feature shape; kind='geojson'."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        doc = json.load(fh)
    for f in doc.get("features") or []:
        if not isinstance(f, dict):
            yield {"family": None, "geom": None, "props": {}, "error": "not a Feature object"}
            continue
        geom = f.get("geometry")
        gtype = geom.get("type") if isinstance(geom, dict) else None
        family = _GEOJSON_FAMILY.get(gtype)
        props = f.get("properties") or {}
        if family is None or not isinstance(geom, dict) or not geom.get("coordinates"):
            yield {"family": None, "geom": None, "props": props, "error": "missing or unsupported geometry"}
        else:
            yield {"family": family, "geom": geom, "props": props, "error": None}


def _iter_shp(jail: str):
    """pyshp streaming over the extracted set — never the whole file in memory."""
    import shapefile

    from .routers.layer_features import _dbf_value  # the shared dbf coercions

    shp = next((f for f in sorted(os.listdir(jail)) if f.lower().endswith(".shp")), None)
    reader = shapefile.Reader(os.path.join(jail, shp))
    names = [f[0] for f in reader.fields[1:]]
    for sr in reader.iterShapeRecords():
        props = {names[i]: _dbf_value(v) for i, v in enumerate(list(sr.record)) if i < len(names)}
        try:
            geom = sr.shape.__geo_interface__
        except Exception:  # noqa: BLE001 — a malformed shape is a row error, not a crash
            geom = None
        family = _GEOJSON_FAMILY.get(geom.get("type")) if isinstance(geom, dict) else None
        if family is None or not geom.get("coordinates"):
            yield {"family": None, "geom": None, "props": props, "error": "missing or unsupported geometry"}
        else:
            yield {"family": family, "geom": geom, "props": props, "error": None}


def _iter_gpkg(path: str):
    """stdlib sqlite3 cursor streaming; geometry blobs via the shared GPKG header parser.
    kind='wkb'."""
    from .routers.layer_features import _gpkg_geometry

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        # The worker reads every row on the event loop, between its inserts: the same guard as the
        # sniff, with a budget sized for the largest honest import (K1). A refusal or a budget stop
        # raises, and process_job marks the job failed with the sentence, so it is not retried.
        gpkg_guard.guard(con, gpkg_guard.WORKER_BUDGET_OPS)
        gpkg_guard.require_plain_table(con, "gpkg_geometry_columns")
        table, gtype, geom_col = con.execute(
            "SELECT table_name, geometry_type_name, column_name FROM gpkg_geometry_columns LIMIT 1"
        ).fetchone()
        gpkg_guard.require_plain_table(con, table)
        family = _GPKG_FAMILY.get((gtype or "").upper())
        cur = con.execute(f"SELECT * FROM {_sqlite_ident(table)}")
        cols = [d[0] for d in cur.description]
        gi = cols.index(geom_col)
        try:
            for row in cur:
                props = {c: row[i] for i, c in enumerate(cols) if i != gi and c.lower() != "fid"}
                fam, wkb = (None, None)
                if row[gi] is not None:
                    try:
                        fam, wkb = _gpkg_geometry(row[gi])
                    except Exception:  # noqa: BLE001
                        fam, wkb = None, None
                if wkb is None:
                    yield {"family": None, "geom": None, "props": props, "error": "missing or unreadable geometry"}
                else:
                    yield {"family": fam or family, "geom": wkb, "props": props, "error": None}
        except sqlite3.OperationalError as exc:
            if gpkg_guard.is_budget_stop(exc):
                raise gpkg_guard.UnsafeGeoPackage(gpkg_guard.TOO_COSTLY) from None
            raise
    finally:
        con.close()


def _iter_csv(path: str):
    """Streamed rows → GeoJSON Points (kind='geojson', like _iter_shp). Runs the SAME
    _csv_scan the sniff ran, then coerces per COLUMN: numeric-typed columns get _csv_num
    (a cell that still isn't numeric stays a string and the field validator refuses that row
    honestly — the field really is numeric); text-typed columns pass through as raw strings
    (empty ⇒ null), so a numeric-LOOKING cell in a text column stays text — the 2,977-refusal
    lesson in _csv_scan's docstring. A short row, or an x/y cell that doesn't parse, is a
    counted row error — never a crash."""
    headers, xi, yi, _detected, attr_idx, kinds = _csv_scan(path)
    names = [headers[i] for i in attr_idx]
    numeric = {i for i in attr_idx if kinds.get(i) in ("int", "float")}

    def _cell(i: int, row: list[str]):
        if i >= len(row):
            return None
        if i in numeric:
            return _csv_num(row[i])
        s = row[i].strip()
        return s if s != "" else None

    fh, reader = _csv_open(path)
    try:
        next(reader)  # header
        for row in reader:
            if not any(c.strip() for c in row):
                continue  # a fully blank line is CSV lint, not a data row
            props = {names[j]: _cell(i, row) for j, i in enumerate(attr_idx)}
            x = _csv_num(row[xi]) if xi < len(row) else None
            y = _csv_num(row[yi]) if yi < len(row) else None
            # ★ FINITE, not merely numeric. PostGIS accepts POINT(NaN NaN) and POINT(Infinity 1)
            # without complaint — ST_IsValid even says true — and ONE of them turns the layer's
            # whole ST_Extent into BOX(NaN NaN,NaN NaN), breaking zoom-to-layer and the extent
            # picker for every clean feature in it. _csv_num already refuses the non-finite
            # spellings; this is the wall that holds even if it is ever loosened.
            if _csv_finite(x) and _csv_finite(y):
                yield {"family": "point", "geom": {"type": "Point", "coordinates": [x, y]},
                       "props": props, "error": None}
            else:
                yield {"family": None, "geom": None, "props": props,
                       "error": "missing or non-numeric coordinates"}
    finally:
        fh.close()


async def _load_actor(conn: AsyncConnection, user_id: str):
    from .models.user import User

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, username, email, display_name, principal_type FROM platform.users WHERE id = %s",
            (user_id,),
        )
        r = await cur.fetchone()
    if r is None:
        return None
    return User(id=r[0], username=r[1], email=r[2], display_name=r[3], principal_type=r[4] or "human")


async def _mark_failed(conn: AsyncConnection, job_id: str, reason: str) -> None:
    await conn.rollback()
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.import_jobs SET status = 'failed', error = %s, "
            "finished_at = now(), updated_at = now() WHERE id = %s",
            (reason[:500], job_id),
        )
    await conn.commit()
    reap_staging(job_id)


async def process_job(conn: AsyncConnection, job: dict[str, Any]) -> None:
    """Parse the staged file in batches through the E4 machinery. Never raises — a bad job
    is marked failed with an honest first line; the loop lives on (the worker discipline)."""
    from psycopg import errors as pg_errors
    from psycopg import sql
    from psycopg.types.json import Jsonb

    from .audit_context import set_audit_context
    from .layer_props import PropsError, active_field_defs, validate_props
    from .routers.layer_features import _ingest_geom_expr, _slot_table, _storage_srid
    from .routers.layer_fields import _sanitize_key

    job_id = job["id"]
    jail = job_dir(job_id)
    layer_id = job["target_layer_id"]
    fmt = job["format"]
    try:
        upload = os.path.join(
            jail,
            "upload" + STAGED_EXT[fmt],
        )
        if not os.path.isdir(jail) or (fmt != "shp-zip" and not os.path.exists(upload)):
            await _mark_failed(conn, job_id, "staged file missing (api restarted between staging and processing?)")
            return

        actor = await _load_actor(conn, job["created_by"])
        if actor is None:
            await _mark_failed(conn, job_id, "job creator no longer exists")
            return

        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM public.spatial_ref_sys WHERE srid = %s", (job["source_srid"],))
            if await cur.fetchone() is None:
                await _mark_failed(conn, job_id, f"unsupported source CRS (srid {job['source_srid']})")
                return
            await cur.execute(
                "SELECT native_storage, working_srid, geom_type FROM platform.layers WHERE layer_id = %s",
                (layer_id,),
            )
            lrow = await cur.fetchone()
        if lrow is None:
            await _mark_failed(conn, job_id, "target layer no longer exists")
            return
        native, working_srid, geom_type = bool(lrow[0]), int(lrow[1]), lrow[2]
        storage = _storage_srid(native, working_srid)

        field_defs = await active_field_defs(conn, layer_id)
        by_lower = {k.lower(): k for k in field_defs}

        if fmt == "geojson":
            kind, source = "geojson", _iter_geojson(upload)
        elif fmt == "shp-zip":
            kind, source = "geojson", _iter_shp(jail)
        elif fmt == "csv":
            kind, source = "geojson", _iter_csv(upload)
        elif fmt == "kml":
            kind, source = "geojson", _iter_kml(upload, jail)
        else:
            kind, source = "wkb", _iter_gpkg(upload)

        query = sql.SQL(
            "INSERT INTO {tbl} (geom, notes, props, created_by, updated_by) "
            "VALUES ({geom}, %(notes)s, %(props)s, %(actor)s, %(actor)s)"
        ).format(tbl=_slot_table(layer_id), geom=sql.SQL(_ingest_geom_expr(kind, geom_type)))

        processed = int(job["processed_rows"] or 0)
        inserted = int(job["inserted_rows"] or 0)
        failed = int(job["failed_rows"] or 0)
        samples: list[dict[str, Any]] = list(job.get("error_sample") or [])
        skip = processed  # crash-resume: source order is stable (file order)
        idx = -1
        batch: list[dict[str, Any]] = []
        cap = settings.import_max_features

        async def flush() -> bool:
            """One batch = one transaction: audit ctx + INSERT + counters. False = stop."""
            nonlocal inserted, failed, processed
            if not batch:
                return True
            valid: list[dict[str, Any]] = []
            for r in batch:
                if r["error"] is not None:
                    failed += 1
                    if len(samples) < settings.import_error_sample_max:
                        samples.append({"row": r["row"], "reason": r["error"]})
                    continue
                valid.append(r)
            params = [
                {"g": (json.dumps(r["geom"]) if kind == "geojson" else r["geom"]),
                 "src": job["source_srid"], "storage": storage,
                 "notes": r["notes"], "props": Jsonb(r["props"]), "actor": str(actor.id)}
                for r in valid
            ]
            async with conn.cursor() as cur:
                # cancel is honored at the batch boundary — the owner's kill switch
                await cur.execute("SELECT status FROM platform.import_jobs WHERE id = %s", (job_id,))
                st = await cur.fetchone()
                if st is None or st[0] == "canceled":
                    await conn.rollback()
                    reap_staging(job_id)
                    return False
                await set_audit_context(cur, actor, "bulk-import")
                if params:
                    try:
                        await cur.executemany(query, params)
                    except (pg_errors.CheckViolation, pg_errors.InternalError_, pg_errors.DataError):
                        # fall back ROW-PER-TRANSACTION so one bad row costs one verdict, not
                        # the batch — and prior good rows survive (audit GUCs are txn-local,
                        # so the context is re-set inside every row's transaction).
                        await conn.rollback()
                        for p, r in zip(params, valid):
                            try:
                                async with conn.cursor() as cur2:
                                    await set_audit_context(cur2, actor, "bulk-import")
                                    await cur2.execute(query, p)
                                await conn.commit()
                                inserted += 1
                            except (pg_errors.CheckViolation, pg_errors.InternalError_, pg_errors.DataError) as re:
                                await conn.rollback()
                                failed += 1
                                if len(samples) < settings.import_error_sample_max:
                                    samples.append({"row": r["row"], "reason": str(re).splitlines()[0][:200]})
                        processed += len(batch)
                        async with conn.cursor() as cur3:
                            await cur3.execute(
                                "UPDATE platform.import_jobs SET processed_rows=%s, inserted_rows=%s, "
                                "failed_rows=%s, error_sample=%s, updated_at=now() WHERE id=%s",
                                (processed, inserted, failed, Jsonb(samples), job_id),
                            )
                        await conn.commit()
                        del batch[:]
                        return True
                    inserted += len(params)
                processed += len(batch)
                await cur.execute(
                    "UPDATE platform.import_jobs SET processed_rows=%s, inserted_rows=%s, "
                    "failed_rows=%s, error_sample=%s, updated_at=now() WHERE id=%s",
                    (processed, inserted, failed, Jsonb(samples), job_id),
                )
            await conn.commit()
            del batch[:]
            return True

        for raw in source:
            idx += 1
            if idx < skip:
                continue
            if processed + len(batch) >= cap:
                await flush()
                await _mark_failed(conn, job_id, f"feature count exceeds the per-job cap ({cap})")
                return
            r = dict(raw)
            r["row"] = idx
            r["notes"] = None
            if r["error"] is None:
                if r["family"] != geom_type:
                    r["error"] = f"geometry is a {r['family']}, but this is a {geom_type} layer"
                else:
                    props: dict[str, Any] = {}
                    notes = None
                    for k, v in (r["props"] or {}).items():
                        kl = str(k).lower()
                        fk = by_lower.get(kl) or by_lower.get(_sanitize_key(str(k)))
                        if kl == "notes":
                            notes = None if v is None else str(v)
                        elif fk is not None:
                            props[fk] = v
                    try:
                        validate_props(field_defs, props)
                        r["props"], r["notes"] = props, notes
                    except PropsError as e:
                        r["error"] = "; ".join(str(x) for x in e.errors)
            batch.append(r)
            if len(batch) >= _BATCH:
                if not await flush():
                    return  # canceled
        if not await flush():
            return

        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE platform.import_jobs SET status='completed', total_rows=%s, "
                "finished_at=now(), updated_at=now() WHERE id=%s AND status='running'",
                (processed, job_id),
            )
        await conn.commit()
        reap_staging(job_id)
        log.info("import_job_completed", job_id=job_id, layer_id=layer_id,
                 processed=processed, inserted=inserted, failed=failed)
    except Exception as e:  # noqa: BLE001 — the worker never dies on a bad job
        log.warning("import_job_failed", job_id=job_id, exc_info=True)
        try:
            await _mark_failed(conn, job_id, str(e).splitlines()[0][:300] or type(e).__name__)
        except Exception:  # noqa: BLE001
            log.warning("import_job_fail_mark_failed", job_id=job_id, exc_info=True)


async def cancel_job(conn: AsyncConnection, job_id: str, actor: UUID) -> dict[str, Any] | None:
    """pending → canceled immediately (staging reaped); running → canceled flag the worker
    honors at its next batch boundary (it reaps staging at the terminal transition)."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE platform.import_jobs SET status = 'canceled', finished_at = now(), "
            f"updated_at = now() WHERE id = %s AND created_by = %s "
            f"AND status IN ('pending', 'running') RETURNING {_JOB_COLS}",
            (job_id, actor),
        )
        row = await cur.fetchone()
    await conn.commit()
    if row is None:
        return None
    job = _job_dict(row)
    reap_staging(job["id"])
    return job
