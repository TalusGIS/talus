"""Dynamic COG tile rendering via rio-tiler, embedded in the api (V045+, c6).

The serve half of raster store & serve. rio-tiler opens the COG straight off the local
filesystem (decision-object-store-build.md B4 — range-reads of the internal overviews hit
the disk directly, where `/vsis3/` used to cost an HTTP round-trip to MinIO per read) and
renders a single web-mercator tile per request. This runs IN the api (no separate titiler
daemon) to keep the `docker compose up` install simple (decision-raster-store-serve.md F-serve).

Every open goes through `raster_cog.open_stored` — one named driver, a one-file dataset, GDAL's
remote access off — never a bare path (decision-once-over-security-fixes.md, the stored-raster addendum).
A stored object that is not the GeoTIFF the store writes raises StoredRasterRefused (the api answers 409).

Everything here is BLOCKING (GDAL) — call via run_in_threadpool. Render params (band select / rescale /
colormap) are validated + bounded by the router before they reach here; these functions trust them.
"""
from __future__ import annotations

from dataclasses import dataclass

from rasterio.crs import CRS
from rio_tiler.errors import PointOutsideBounds, TileOutsideBounds
from rio_tiler.io import Reader

from .raster_cog import open_stored

WGS84_CRS = CRS.from_epsg(4326)


@dataclass(frozen=True)
class TileJSONMeta:
    minzoom: int
    maxzoom: int
    bounds: list[float]  # [west, south, east, north] in EPSG:4326
    center: list[float]  # [lon, lat, zoom]


def render_tile(
    object_key: str,
    z: int,
    x: int,
    y: int,
    indexes: list[int],
    rescale: list[tuple[float, float]] | None,
    colormap_name: str | None,
) -> bytes | None:
    """Render one z/x/y PNG tile from the COG. Returns None if the tile is outside the raster's
    footprint (the router turns that into 204). BLOCKING."""
    with open_stored(object_key) as src:
        with Reader(src.name, dataset=src) as r:
            try:
                img = r.tile(x, y, z, indexes=tuple(indexes), tilesize=256)
            except TileOutsideBounds:
                return None
            if rescale:
                img.rescale(in_range=rescale)
            colormap = None
            if colormap_name:
                from rio_tiler.colormap import cmap

                colormap = cmap.get(colormap_name)
            return img.render(img_format="PNG", colormap=colormap)


def read_point(object_key: str, lon: float, lat: float) -> list[float | None] | None:
    """Sample the band value(s) at a WGS84 lon/lat — the raster half of the multi-layer identify popup
    (decision-multi-layer-popup.md). Returns one value per band (None for a masked / nodata pixel), or
    None when the point is outside the raster's footprint (the router turns that into outOfBounds). A
    windowed read of a single pixel off the COG — display/inspection, NOT analytics. BLOCKING."""
    with open_stored(object_key) as src:
        with Reader(src.name, dataset=src) as r:
            try:
                pt = r.point(lon, lat, coord_crs=WGS84_CRS)
            except PointOutsideBounds:
                return None
            # PointData.array is a 1-D masked array (one entry per band); .tolist() yields plain
            # floats/ints with None for masked (nodata) entries — JSON-ready.
            return pt.array.tolist()


@dataclass(frozen=True)
class NativeMeta:
    """The COG's native-CRS facts a WCS DescribeCoverage needs (I-6). `epsg` is None when the CRS
    has no EPSG code — such rasters are omitted from the WCS façade (clients parse `EPSG:n`
    srsNames reliably; WKT srsNames are legal WCS but a client-compat minefield)."""

    epsg: int | None
    bounds: tuple[float, float, float, float]  # (left, bottom, right, top), native CRS
    width: int
    height: int
    band_count: int
    dtype: str
    nodata: float | None
    transform: tuple[float, float, float, float, float, float]  # GDAL affine (a,b,c,d,e,f)
    lonlat_bounds: list[float]  # [west, south, east, north] EPSG:4326, for lonLatEnvelope


def native_meta(object_key: str) -> NativeMeta:
    """Native-CRS metadata off the COG header — no pixel reads. BLOCKING."""
    from rasterio.warp import transform_bounds

    with open_stored(object_key) as src:
        w, s, e, n = transform_bounds(src.crs, WGS84_CRS, *src.bounds, densify_pts=21)
        t = src.transform
        return NativeMeta(
            epsg=src.crs.to_epsg() if src.crs else None,
            bounds=tuple(src.bounds),
            width=src.width,
            height=src.height,
            band_count=src.count,
            dtype=src.dtypes[0],
            nodata=src.nodata,
            transform=(t.a, t.b, t.c, t.d, t.e, t.f),
            lonlat_bounds=[w, s, e, n],
        )


def tilejson_meta(object_key: str) -> TileJSONMeta:
    """The COG's zoom range + WGS84 bounds, for a TileJSON doc and the map's zoom-to-extent. BLOCKING."""
    with open_stored(object_key) as src:
        with Reader(src.name, dataset=src) as r:
            # rio-tiler 7: geographic_bounds is now a method taking the output CRS.
            w, s, e, n = r.get_geographic_bounds(WGS84_CRS)
            return TileJSONMeta(
                minzoom=int(r.minzoom),
                maxzoom=int(r.maxzoom),
                bounds=[w, s, e, n],
                center=[(w + e) / 2, (s + n) / 2, int(r.minzoom)],
            )


def read_thumbnail_or_tile_dims(object_key: str) -> dict:
    """Lightweight info for the map UI: bounds + zoom range (no per-band statistics read, which would
    touch pixels). BLOCKING."""
    meta = tilejson_meta(object_key)
    return {
        "bounds": meta.bounds,
        "center": meta.center,
        "minzoom": meta.minzoom,
        "maxzoom": meta.maxzoom,
    }
