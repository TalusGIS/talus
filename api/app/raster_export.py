"""Raster clip-and-download — windowed read + GIS-format write (V050+, decision-raster-export.md).

The export half of raster store & serve, the raster analog of T33 Open Data's vector `/export`. A
viewer draws a bbox on the map; we range-read JUST that window straight off the on-disk COG (cheap —
it decimates through the COG's internal overviews, the same reason the serve path requires a COG) and
write it to a real, georeferenced GIS file the caller takes into their OWN QGIS/ArcGIS/QField. Clip +
reformat only — NO reprojection-as-a-service (we preserve the native CRS), NO resampling/analysis knobs,
NO mosaicking. Those are the deferred / paid-tier lines (decision §9/§10).

Everything here is BLOCKING (GDAL) — call via run_in_threadpool.

Output is bounded: a window whose NATIVE-resolution pixel count exceeds the configured megapixel cap is
REFUSED (ExportTooLarge, -> 413) unless the caller opts into downsampling via `max_dim`. Honest by
design — a file the UI calls "GIS-grade" is never silently degraded (decision §3 F-size-bound).

Writable formats are PROBED at import against the running GDAL (rasterio's bundled wheel), not assumed:
GeoTIFF + COG are guaranteed; ERDAS IMG / JPEG 2000 are advertised only if this GDAL can actually write
them. The route serves `SUPPORTED_EXPORT_FORMATS` and 400s anything outside it.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

import numpy as np
import rasterio
import rasterio.shutil
import structlog
from affine import Affine
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

from .raster_cog import open_stored

log = structlog.get_logger(__name__)


class ExportTooLarge(Exception):
    """The window at native resolution exceeds the megapixel cap and no downsample was requested."""

    def __init__(self, native_width: int, native_height: int, cap_megapixels: int) -> None:
        self.native_width = native_width
        self.native_height = native_height
        self.cap_megapixels = cap_megapixels
        super().__init__(
            f"selected area is {native_width}x{native_height} px "
            f"(~{native_width * native_height / 1e6:.1f} MP) at native resolution, over the "
            f"{cap_megapixels} MP export cap"
        )


class ExportEmpty(Exception):
    """The requested bbox does not overlap the raster footprint — nothing to export."""


@dataclass(frozen=True)
class FormatSpec:
    key: str          # query value, e.g. 'cog'
    label: str        # human label for the UI dropdown
    driver: str       # GDAL driver, e.g. 'COG'
    ext: str          # file extension, e.g. 'tif'
    media_type: str   # response Content-Type
    create_copy: bool # True => write via rasterio.shutil.copy (CreateCopy-only drivers, e.g. COG)


# Candidate formats (decision §3 F-formats). GeoTIFF + COG are the lossless georeferenced default;
# ERDAS IMG + JPEG 2000 are offered if writable. ASCII grid (single-band only) and PNG/JPEG (8-bit
# pictures, sidecar-only georef) are deliberately EXCLUDED — not GIS-grade for float/16-bit/RGB rasters.
_CANDIDATES = (
    FormatSpec("cog", "Cloud-Optimized GeoTIFF", "COG", "tif", "image/tiff", create_copy=True),
    FormatSpec("geotiff", "GeoTIFF", "GTiff", "tif", "image/tiff", create_copy=False),
    FormatSpec("img", "ERDAS IMAGINE (.img)", "HFA", "img", "application/octet-stream", create_copy=True),
    FormatSpec("jp2", "JPEG 2000 (.jp2)", "JP2OpenJPEG", "jp2", "image/jp2", create_copy=True),
)


def _probe_writable(spec: FormatSpec) -> bool:
    """Definitively test that the running GDAL can WRITE this driver, by copying a 1x1 GeoTIFF into it
    once at import. Cheaper and more honest than guessing from the wheel's driver list."""
    if spec.driver == "GTiff":
        return True  # the base format; rasterio cannot exist without it
    src_fd, src_path = tempfile.mkstemp(suffix=".tif")
    os.close(src_fd)
    dst_path = src_path + f".probe.{spec.ext}"
    try:
        with rasterio.open(
            src_path, "w", driver="GTiff", height=1, width=1, count=1,
            # a trivial but NON-identity transform — Affine.identity() trips GDAL's
            # NotGeoreferencedWarning, and this probe runs at startup (keep boot logs clean).
            dtype="uint8", crs="EPSG:4326", transform=Affine.translation(0, 1) * Affine.scale(1, -1),
        ) as dst:
            dst.write(np.zeros((1, 1, 1), "uint8"))
        rasterio.shutil.copy(src_path, dst_path, driver=spec.driver)
        return True
    except Exception as exc:  # noqa: BLE001 — an unwritable driver just drops out of the offered set
        log.info("raster_export_format_unavailable", driver=spec.driver, detail=str(exc))
        return False
    finally:
        for p in (src_path, dst_path):
            try:
                os.unlink(p)
            except OSError:
                pass


SUPPORTED_EXPORT_FORMATS: dict[str, FormatSpec] = {
    s.key: s for s in _CANDIDATES if _probe_writable(s)
}


def _output_dims(native_w: int, native_h: int, cap_megapixels: int, max_dim: int | None) -> tuple[int, int]:
    """Resolve output pixel dims under the cap. Over-cap with no max_dim -> ExportTooLarge; with max_dim,
    downsample so the longest side <= max_dim (and never below 1). max_dim also caps an under-cap window
    if the caller asked for a smaller file."""
    cap_px = cap_megapixels * 1_000_000
    over_cap = native_w * native_h > cap_px
    if over_cap and max_dim is None:
        raise ExportTooLarge(native_w, native_h, cap_megapixels)
    scale = 1.0
    if max_dim is not None and max(native_w, native_h) > max_dim:
        scale = max_dim / max(native_w, native_h)
    if over_cap:
        # ensure the downsampled result also clears the cap (max_dim might still be large)
        cap_scale = (cap_px / (native_w * native_h)) ** 0.5
        scale = min(scale, cap_scale)
    out_w = max(1, round(native_w * scale))
    out_h = max(1, round(native_h * scale))
    return out_w, out_h


def coverage_to_geotiff(
    object_key: str,
    bbox_native: tuple[float, float, float, float],
    req_w: int,
    req_h: int,
    cap_megapixels: int,
) -> tuple[str, int, int]:
    """WCS GetCoverage read (interop I-6, decision-interop-i6-coverages.md): `bbox_native` is
    (left, bottom, right, top) ALREADY in the raster's native CRS (we advertise no other), and the
    client names its output grid (req_w × req_h — WCS clients request the map window at screen
    resolution). Same windowed COG-overview read as clip_to_file, minus the CRS transform and the
    format fan-out: output is always a plain GeoTIFF.

    Bounds discipline: the REQUESTED grid over the cap -> ExportTooLarge (the router 413s); output
    dims are clamped to the window's native pixel count — we downsample for display (overview
    decimation, Resampling.average) but NEVER upsample past native (values stay honest; the client
    resamples for its own screen). Returns (path, out_w, out_h). Caller unlinks. BLOCKING."""
    cap_px = cap_megapixels * 1_000_000
    if req_w * req_h > cap_px:
        raise ExportTooLarge(req_w, req_h, cap_megapixels)
    with open_stored(object_key) as src:
        left, bottom, right, top = bbox_native
        window = from_bounds(left, bottom, right, top, transform=src.transform)
        window = window.round_offsets().round_lengths()
        col_off = max(int(window.col_off), 0)
        row_off = max(int(window.row_off), 0)
        col_end = min(int(window.col_off) + int(window.width), src.width)
        row_end = min(int(window.row_off) + int(window.height), src.height)
        if col_end <= col_off or row_end <= row_off:
            raise ExportEmpty()
        window = Window(col_off, row_off, col_end - col_off, row_end - row_off)
        native_w, native_h = int(window.width), int(window.height)

        out_w, out_h = min(req_w, native_w), min(req_h, native_h)
        downsampled = (out_w != native_w) or (out_h != native_h)
        resampling = Resampling.average if downsampled else Resampling.nearest

        data = src.read(window=window, out_shape=(src.count, out_h, out_w), resampling=resampling)
        out_transform = src.window_transform(window)
        if downsampled:
            out_transform = out_transform * Affine.scale(native_w / out_w, native_h / out_h)

        profile = {
            "driver": "GTiff", "width": out_w, "height": out_h, "count": src.count,
            "dtype": src.dtypes[0], "crs": src.crs, "transform": out_transform,
        }
        if src.nodata is not None:
            profile["nodata"] = src.nodata
        out_fd, out_path = tempfile.mkstemp(suffix=".tif")
        os.close(out_fd)
        try:
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(data)
                # STATISTICS_* per band — the fix for "WCS layer won't RENDER in ArcGIS Pro"
                # (rig leg 2, 2026-07-18: *"Update Colorizer failed!"*, opaque all-white).
                # Pro builds its stretch from band statistics; with none it cannot construct a
                # colorizer at all, and the layer draws blank even though every pixel value is
                # correct (Identify was verified exact against ground truth in the same session).
                # The source COG carries these tags, but a windowed read is a NEW raster and GDAL
                # does not synthesise them — so we compute them over exactly the array we are
                # about to hand back, which also makes the stretch right for THIS window rather
                # than for the whole coverage.
                #
                # nodata is excluded, or a -999999 fill would drag the minimum down and flatten
                # the visible range to nothing — the same class of defect as the unmasked fill
                # the finding describes. An all-nodata window simply gets no tags: no statistics
                # is honest, fabricated ones are not.
                for bidx in range(1, src.count + 1):
                    band = data[bidx - 1]
                    valid = band if src.nodata is None else band[band != src.nodata]
                    valid = valid[np.isfinite(valid)]
                    if valid.size == 0:
                        continue
                    dst.update_tags(
                        bidx,
                        STATISTICS_MINIMUM=repr(float(valid.min())),
                        STATISTICS_MAXIMUM=repr(float(valid.max())),
                        STATISTICS_MEAN=repr(float(valid.mean())),
                        STATISTICS_STDDEV=repr(float(valid.std())),
                        STATISTICS_VALID_PERCENT=repr(round(100.0 * valid.size / band.size, 2)),
                    )
        except Exception:
            try:
                os.unlink(out_path)
            except OSError:
                pass
            raise
        return out_path, out_w, out_h


def clip_to_file(
    object_key: str,
    bbox: tuple[float, float, float, float] | None,
    spec: FormatSpec,
    cap_megapixels: int,
    max_dim: int | None,
) -> tuple[str, int, int, bool]:
    """Read `bbox` (EPSG:4326; None = whole raster) from the COG and write it to a temp file in
    `spec`'s format, preserving the NATIVE CRS. Returns (path, native_w, native_h, downsampled).
    Raises ExportTooLarge / ExportEmpty. The caller streams the file and unlinks it. BLOCKING."""
    with open_stored(object_key) as src:
        full = Window(0, 0, src.width, src.height)
        if bbox is None:
            window = full
        else:
            w, s, e, n = bbox
            # densify the bbox edges when reprojecting to the native CRS so a rotated/curved
            # projection still encloses the drawn rectangle.
            left, bottom, right, top = transform_bounds(
                "EPSG:4326", src.crs, w, s, e, n, densify_pts=21
            )
            window = from_bounds(left, bottom, right, top, transform=src.transform)
            window = window.round_offsets().round_lengths()
            # Clamp to the raster extent manually (version-robust — Window.intersection isn't in
            # every rasterio). Disjoint -> empty.
            col_off = max(int(window.col_off), 0)
            row_off = max(int(window.row_off), 0)
            col_end = min(int(window.col_off) + int(window.width), src.width)
            row_end = min(int(window.row_off) + int(window.height), src.height)
            if col_end <= col_off or row_end <= row_off:
                raise ExportEmpty()
            window = Window(col_off, row_off, col_end - col_off, row_end - row_off)
        native_w, native_h = int(round(window.width)), int(round(window.height))
        if native_w < 1 or native_h < 1:
            raise ExportEmpty()

        out_w, out_h = _output_dims(native_w, native_h, cap_megapixels, max_dim)
        downsampled = (out_w != native_w) or (out_h != native_h)
        resampling = Resampling.average if downsampled else Resampling.nearest

        data = src.read(window=window, out_shape=(src.count, out_h, out_w), resampling=resampling)
        out_transform = src.window_transform(window)
        if downsampled:
            out_transform = out_transform * Affine.scale(native_w / out_w, native_h / out_h)

        # Build a CLEAN profile rather than copying src.profile — a COG's profile carries
        # tiled=True + blockxsize/blockysize=512, which makes GDAL refuse a sub-block-size clip
        # (e.g. a 100x100 box), plus photometric/interleave keys that can clash. The intermediate
        # is a plain striped GTiff; the final driver applies its own layout on CreateCopy.
        profile = {
            "driver": "GTiff", "width": out_w, "height": out_h, "count": src.count,
            "dtype": src.dtypes[0], "crs": src.crs, "transform": out_transform,
        }
        if src.nodata is not None:
            profile["nodata"] = src.nodata
        # COG/IMG/JP2 are CreateCopy-only or copy-friendly: write a plain GTiff first, then copy.
        gtiff_fd, gtiff_path = tempfile.mkstemp(suffix=".tif")
        os.close(gtiff_fd)
        try:
            with rasterio.open(gtiff_path, "w", **profile) as dst:
                dst.write(data)
            if not spec.create_copy:
                return gtiff_path, native_w, native_h, downsampled
            out_fd, out_path = tempfile.mkstemp(suffix=f".{spec.ext}")
            os.close(out_fd)
            try:
                # The one read by path left in this module: `gtiff_path` is the temporary GeoTIFF written just
                # above from pixel arrays and a profile built here — nothing of the stored object's own
                # metadata travels into it — and the copy runs inside open_stored's GDAL settings.
                rasterio.shutil.copy(gtiff_path, out_path, driver=spec.driver)
            finally:
                try:
                    os.unlink(gtiff_path)
                except OSError:
                    pass
            return out_path, native_w, native_h, downsampled
        except Exception:
            try:
                os.unlink(gtiff_path)
            except OSError:
                pass
            raise
