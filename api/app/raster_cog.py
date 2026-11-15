"""COG validation + metadata extraction + thumbnail, via rio-cogeo / rasterio / rio-tiler (V045+).

Every function here is BLOCKING (GDAL) — call via run_in_threadpool. The api REQUIRES a COG up front
(decision-raster-store-serve.md F-ingest): at ingest we validate it IS a Cloud-Optimized GeoTIFF with a
usable CRS, extract the placement metadata the catalog/map need, and render a small thumbnail — so the
serve path (c6) only ever opens known-good COGs and the V045 `rasters_ready_metadata_chk` always holds.

★ AN UPLOAD IS OPENED WITH ONE NAMED DRIVER, NEVER "WHATEVER GDAL RECOGNISES"
(decision-once-over-security-fixes.md, A1). GDAL chooses a driver from a file's CONTENT, and this
build registers ~150 of them. Several treat the file as a list of OTHER places to read: a VRT
document's `<SourceFilename>` can be another file on the volume or `/vsicurl/http://…`, and the WMS,
tile-index and HTTP drivers are the same idea. Posted under a `.tif` name, such a document made the
server read what it pointed at and hand the result back as a raster. So:

  1. `sniff_driver` reads the file's first bytes IN PYTHON and maps the signature to exactly one
     driver. No signature we accept → refused before GDAL is called at all.
  2. Every open here passes that one driver (`rasterio.open(path, driver=<name>)`). rasterio 1.5 takes
     a single name only — a list raises TypeError — which is why the driver is chosen first.
  3. A file that names an "overview file" in its own metadata is refused, and the opened dataset
     must consist of that one file (`_open_one_file`). The first half exists because a GENUINE GeoTIFF
     can carry `OVERVIEW_FILE` in an `OVERVIEWS` metadata domain, and GDAL then opens that target —
     another file, an http:// address, an inline WMS description — with EVERY driver, the first time
     the file list or the overviews are asked for. Found by the independent review of this fix; the
     single-driver open does not reach it.
  4. The whole path runs inside `_upload_env()`: GDAL's remote-file handler refuses every address
     (the suffix it would accept is random per process, so a crafted address cannot carry it).
  5. The caller is told a fixed sentence; GDAL's own error text goes to the log, never to the response.

Do not add an open by bare path to this module, or to `raster_serve.py` / `raster_export.py`.

★ A STORED RASTER IS OPENED THE SAME WAY (`open_stored`). The store holds only what this module
wrote, and it writes GeoTIFFs (every upload is converted to a COG). But an object can reach the
store by another road: a raster uploaded before the rules above existed, or an older server's raster
folder put back by a restore. So the tile, point, metadata and export paths do not trust "stored
means safe": they open through `open_stored`, which applies points 1 to 4 with the one driver the
store writes, and answers anything else with one fixed sentence (`StoredRasterRefused`).
"""
from __future__ import annotations

import secrets
from contextlib import contextmanager
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)

# What the caller is told. Fixed text on purpose (point 5 above).
_MSG_UNSUPPORTED = (
    "could not read this file as a raster. Supported formats are GeoTIFF/COG, JPEG 2000, and ERDAS IMG."
)
_MSG_UNREADABLE = "could not read this raster: the file is damaged or incomplete."
_MSG_STORED = "this raster's stored file cannot be opened. Upload the raster again."
# The one driver the raster store's objects are written with (`to_cog` writes a COG, which is a GeoTIFF).
_STORED_DRIVER = "GTiff"

# Signature (offset, bytes) -> the ONE driver allowed to open a file that starts with it. These are
# the formats the upload picker offers (.tif/.tiff/.cog, .jp2, .img). Adding a format means adding
# its signature here AND proving, in tests/test_raster_upload_drivers.py, that its driver cannot be
# pointed at another file or address by the file's own contents.
_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"II*\x00", "GTiff"),            # TIFF, little-endian
    (0, b"MM\x00*", "GTiff"),            # TIFF, big-endian
    (0, b"II+\x00", "GTiff"),            # BigTIFF, little-endian
    (0, b"MM\x00+", "GTiff"),            # BigTIFF, big-endian
    (0, b"\x00\x00\x00\x0cjP  \r\n\x87\n", "JP2OpenJPEG"),  # JPEG 2000, JP2 container
    (0, b"\xff\x4f\xff\x51", "JP2OpenJPEG"),                # JPEG 2000, raw codestream
    (0, b"EHFA_HEADER_TAG", "HFA"),      # ERDAS IMAGINE
)
_SNIFF_BYTES = 16


class NotACog(Exception):
    """Uploaded file is not a valid COG (rio-cogeo errors) or lacks a usable EPSG CRS — a 400, not a 500."""


class StoredRasterRefused(Exception):
    """A stored raster object is not the single GeoTIFF the store writes, so it is not opened. The
    message is fixed text; the api answers it with 409 (main.py)."""


@dataclass(frozen=True)
class RasterInfo:
    srid: int
    extent_wkt: str  # POLYGON in EPSG:4326 (for platform.rasters.extent)
    band_count: int
    dtype: str
    width: int
    height: int
    pixel_size: float
    default_render: dict


def sniff_driver(path: str) -> str | None:
    """The one GDAL driver allowed to open `path`, from its leading bytes — or None. Pure Python: no
    GDAL call, so a file we do not accept is never parsed by anything that follows references."""
    try:
        with open(path, "rb") as f:
            head = f.read(_SNIFF_BYTES)
    except OSError:
        return None
    for offset, magic, driver in _SIGNATURES:
        if head[offset:offset + len(magic)] == magic:
            return driver
    return None


# GDAL options for the upload path. The remote-file handler (/vsicurl/ and friends) checks a URL's
# extension against CPL_VSIL_CURL_ALLOWED_EXTENSIONS before it connects; an extension nothing has
# makes it refuse every address. It is a SUFFIX match the caller could satisfy if they knew it, so
# it is random per process and never appears in a response. Sidecar discovery is off (EMPTY_DIR) so a sibling `.ovr`/`.aux.xml`
# is never attached. The timeouts only bound the damage if a future GDAL ignores the first option.
_UPLOAD_GDAL_OPTIONS = {
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".talus-none-" + secrets.token_hex(16),
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_CONNECTTIMEOUT": "1",
    "GDAL_HTTP_TIMEOUT": "1",
    "GDAL_HTTP_MAX_RETRY": "0",
}


def _upload_env():
    import rasterio

    return rasterio.Env(**_UPLOAD_GDAL_OPTIONS)


@contextmanager
def _open_one_file(path: str, driver: str | None, *, refused, unsupported: str, unreadable: str, event: str):
    """The only way this api opens a raster file: signature → one driver → a one-file dataset, with
    GDAL's remote access off. `refused` is the exception class raised (with fixed text) for anything
    else; `event` names the log line that carries the reason, which the caller is never told."""
    import rasterio

    sniffed = sniff_driver(path)
    if sniffed is None or (driver is not None and sniffed != driver):
        log.info(event, reason="signature_not_accepted", expected=driver, sniffed=sniffed)
        raise refused(unsupported)
    with _upload_env():
        try:
            src = rasterio.open(path, driver=sniffed)
        except Exception as exc:  # noqa: BLE001 — the driver refused it: a bad input, not a 500
            log.info(event, reason="driver_refused", driver=sniffed,
                     error=exc.__class__.__name__, detail=str(exc)[:300])
            raise refused(unreadable) from None
        with src:
            # BEFORE anything that makes GDAL look for overviews (`.files`, `.overviews`, a
            # downsampled read): a file that names its own overview source is refused unread.
            # Reading the metadata domain itself opens nothing (tests count connections).
            # The test is whether the metadata domain EXISTS, not what its items say: rasterio's
            # `tags()` leaves out an item it cannot decode as UTF-8, so a reference written as other
            # bytes would read as "no tags" while GDAL still held it and followed it. A GeoTIFF's
            # own internal overviews do not create this domain (tests hold both facts).
            # The domain's name is compared WITHOUT regard to case: GDAL finds the domain whatever
            # case it was written in, and the list returns it as written (once-over 2026-11-15 F1).
            if any((ns or "").upper() == "OVERVIEWS" for ns in (src.tag_namespaces() or [])) \
                    or src.tags(ns="OVERVIEWS"):
                log.warning(event, reason="names_an_external_overview_file", driver=sniffed)
                raise refused(unsupported)
            files = [str(f) for f in src.files]
            if src.driver != sniffed or files != [str(path)]:
                log.warning(event, reason="not_a_single_file_dataset",
                            driver=src.driver, file_count=len(files))
                raise refused(unsupported)
            yield src


def _open_upload(path: str, driver: str | None = None):
    """Open an upload: raises NotACog (fixed text) for anything `_open_one_file` does not accept.
    `driver` pins the expectation when the caller already knows it (a file we just wrote as a COG
    must be a GeoTIFF)."""
    return _open_one_file(path, driver, refused=NotACog, unsupported=_MSG_UNSUPPORTED,
                          unreadable=_MSG_UNREADABLE, event="raster_upload_refused")


def open_stored(object_key: str):
    """Open an object of the raster store for the tile, point, metadata and export paths. The same
    checks as an upload, with the one driver the store writes; raises StoredRasterRefused (fixed
    text) for anything else — a document that points elsewhere, a file that names an overview
    source, a damaged or missing file. Use as `with open_stored(key) as src:`; for rio-tiler,
    `Reader(src.name, dataset=src)`. BLOCKING."""
    from .raster_store import local_path

    return _open_one_file(local_path(object_key), _STORED_DRIVER, refused=StoredRasterRefused,
                          unsupported=_MSG_STORED, unreadable=_MSG_STORED, event="raster_stored_refused")


def inspect_raster(path: str) -> RasterInfo:
    """Open an uploaded raster and extract placement metadata. Raises NotACog if it is not one of the
    supported formats, cannot be read, or lacks an EPSG CRS (we can't place it on a web map). Does NOT
    require a COG — a non-COG upload is converted by to_cog() before storage. BLOCKING."""
    from rasterio.warp import transform_bounds

    # A non-raster / unsupported proprietary format (MrSID, ECW) fails in _open_upload → a clean 400,
    # never a 500.
    try:
        with _open_upload(path) as src:
            if src.crs is None:
                raise NotACog("raster has no CRS")
            epsg = src.crs.to_epsg()
            if epsg is None:
                raise NotACog("raster CRS has no EPSG code (reproject to a standard CRS before upload)")
            b = src.bounds
            minx, miny, maxx, maxy = transform_bounds(src.crs, "EPSG:4326", b.left, b.bottom, b.right, b.top)
            count = int(src.count)
            # Default display: first 3 bands as RGB if available, else the single band. Rescale/colormap
            # are refined in the map UI (c8); opacity is the one client-side knob.
            bidx = [1, 2, 3] if count >= 3 else [1]
            return RasterInfo(
                srid=epsg,
                extent_wkt=(
                    f"POLYGON(({minx} {miny},{minx} {maxy},{maxx} {maxy},{maxx} {miny},{minx} {miny}))"
                ),
                band_count=count,
                dtype=str(src.dtypes[0]),
                width=int(src.width),
                height=int(src.height),
                pixel_size=float(abs(src.res[0])),
                default_render={"bidx": bidx, "opacity": 1.0},
            )
    except NotACog:
        raise
    except Exception as exc:  # noqa: BLE001 — any GDAL/metadata failure is a bad input, not a 500
        log.info("raster_upload_refused", reason="metadata_unreadable",
                 error=exc.__class__.__name__, detail=str(exc)[:300])
        raise NotACog(_MSG_UNREADABLE) from None


def is_cog(path: str) -> bool:
    """True iff `path` is ALREADY a valid Cloud-Optimized GeoTIFF (so it can be stored as-is, no
    conversion). Any read/parse error → False (treat as needs-conversion). BLOCKING.

    rio-cogeo's validator opens the path itself, with every driver. It is therefore reached ONLY for a
    file that carries a TIFF signature and that the GeoTIFF driver has just opened as a single file —
    a file GDAL's content sniffing cannot hand to a different driver (the VRT and tile-index drivers
    recognise text at byte 0; a TIFF signature has a zero byte in its first four) — and it runs with
    remote access off."""
    try:
        with _open_upload(path, driver="GTiff"):
            pass
        from rio_cogeo.cogeo import cog_validate

        valid, _errors, _warnings = cog_validate(path, config=dict(_UPLOAD_GDAL_OPTIONS), quiet=True)
        return bool(valid)
    except Exception:  # noqa: BLE001
        return False


def to_cog(src_path: str, dst_path: str) -> None:
    """Convert a supported raster at `src_path` to a COG at `dst_path` (DEFLATE, tiled, with
    overviews — lossless, universally serveable). Uses GDAL's NATIVE COG driver via CreateCopy
    (rasterio.shutil.copy): it streams block-by-block in GDAL's C layer (no whole-raster load), builds
    overviews automatically, and — unlike rio-cogeo's cog_translate — converts JPEG 2000 sources
    cleanly (cog_translate raised "Cannot invert geotransform" on a valid JP2). The source CRS is kept;
    rio-tiler reprojects to web-mercator at serve time, so no ingest reprojection is needed. BLOCKING.

    The source is handed over as an OPEN dataset (from _open_upload), never as a path: a path would be
    re-opened by GDAL with every driver."""
    import rasterio.shutil

    with _open_upload(src_path) as src:
        rasterio.shutil.copy(
            src, dst_path, driver="COG", compress="DEFLATE", overview_resampling="average"
        )


def make_thumbnail_png(path: str, bidx: list[int], max_size: int = 256) -> bytes | None:
    """Best-effort small PNG preview for the catalog (reads via the COG's overviews, not full res).
    Returns None on ANY failure — a thumbnail is nice-to-have and must never block ingest. BLOCKING.
    `path` is the COG about to be stored, so it must open as a GeoTIFF; rio-tiler is given the open
    dataset rather than the path, for the reason in to_cog."""
    try:
        from rio_tiler.io import Reader

        with _open_upload(path, driver="GTiff") as src:
            with Reader(path, dataset=src) as r:
                img = r.preview(indexes=tuple(bidx), max_size=max_size)
                return img.render(img_format="PNG")
    except Exception:  # noqa: BLE001 — any render failure degrades to "no thumbnail", never fatal
        return None
