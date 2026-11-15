"""Raster store & serve — ingest + item detail (V045+, decision-raster-store-serve.md, c5).

A raster is stored out-db as a Cloud-Optimized GeoTIFF (COG) on the storage volume, served dynamically by
rio-tiler embedded in the api (serve path = c6). Ingest accepts three formats only (GeoTIFF/COG,
JPEG 2000, ERDAS IMG; see raster_cog.py): an already-COG upload is stored as-is, anything else is converted to a COG
here. COG is the internal storage/serve format, not the upload requirement. MrSID/ECW (proprietary)
are out of scope — a paid-tier feature. One synchronous request, no async worker (big-file async
conversion is a later step).

Upload goes THROUGH the api (raw COG bytes as the request body), NOT via a presigned URL: the store
is a volume only the api mounts and is never browser-exposed (decision-16 Q2) — there is no endpoint a
presigned URL could even name. The api streams the body to a temp file (bounded by disk, not memory),
validates with GDAL, then puts it to the store — mirroring how the media proxy moves photo bytes.
Raw-body + query-param metadata keeps ingest dependency-free (no python-multipart).

Authz mirrors web_map: minting an item is publisher/admin (F3); the OpenFGA `raster` object type carries
owner / share / can_view. Create is OpenFGA-FIRST (uuid -> owner tuple -> store -> row), so a mid-failure
leaves a harmless orphan tuple, not an unopenable row. On a later failure the durable state (objects +
tuple) is rolled back so no half-item survives.
"""
import os
import re
import tempfile
from typing import Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from .. import admin_audit
from ..auth import _validated_claims, get_current_user, get_optional_user
from ..authz import (
    OPEN_DATA_GROUP,
    PUBLIC_SYSTEM_GROUPS,
    _has_admin_role,
    _has_publish_role,
    _openfga_check,
    _openfga_delete,
    _openfga_list_objects,
    _openfga_read_object_tuples,
    _openfga_read_user_groups,
    _openfga_write,
    assert_may_share_public,
    get_object_shares,
    public_object_allowed,
    set_object_public,
    set_object_shares,
)
from ..config import settings
from ..db import get_db
from ..media import MediaObjectMissing, MediaStoreUnavailable
from ..models.user import User
from ..raster_cog import NotACog, inspect_raster, is_cog, make_thumbnail_png, to_cog
from ..raster_export import (
    SUPPORTED_EXPORT_FORMATS,
    ExportEmpty,
    ExportTooLarge,
    clip_to_file,
)
from ..raster_serve import read_point, read_thumbnail_or_tile_dims, render_tile, tilejson_meta
from .metadata import metadata_complete
from ..raster_store import put_bytes, put_file, read_object, remove_prefix

log = structlog.get_logger(__name__)

router = APIRouter(tags=["rasters"])

_MAX_TITLE_LEN = 200
_UPLOAD_CHUNK = 1024 * 1024  # 1 MiB stream chunks


def _clean_title(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="title must not be empty")
    if len(v) > _MAX_TITLE_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"title must be at most {_MAX_TITLE_LEN} characters",
        )
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="title must not contain control characters"
        )
    return v


def _as_uuid(raster_id: str) -> str:
    """Reject a non-UUID id with 404 (existence-hiding) before it reaches a uuid column or OpenFGA."""
    try:
        UUID(raster_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="raster not found")
    return raster_id


async def _require_publisher(
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> User:
    """403 unless the caller holds talus-publisher or talus-admin — minting an item, same gate as
    create_web_map / create_layer (F3)."""
    if not _has_publish_role(claims):
        log.info(
            "authz_rejected", capability="rasters:create",
            reason="missing_publish_role", user_id=str(user.id),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to create rasters")
    return user


async def _spool_body(request: Request, cap_bytes: int) -> tuple[str, int]:
    """Stream the request body to a temp file, enforcing the size cap as we go. Returns (path, size).
    413 over cap; 400 on an empty body. The temp file is the COG GDAL then validates + uploads."""
    fd, path = tempfile.mkstemp(suffix=".tif")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > cap_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"raster exceeds the {settings.raster_max_upload_mb} MB upload limit",
                    )
                out.write(chunk)
    except BaseException:
        _silent_unlink(path)
        raise
    if size == 0:
        _silent_unlink(path)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty request body — POST the COG bytes as the request body",
        )
    return path, size


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


@router.post("/rasters", status_code=status.HTTP_201_CREATED, summary="Upload a COG raster (publisher only)")
async def create_raster(
    request: Request,
    title: str,
    description: str | None = None,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(_require_publisher),
) -> dict[str, Any]:
    """Ingest a COG: stream body -> GDAL-validate -> store -> `ready` row. The COG bytes are
    the request body; `title`/`description` are query params (keeps ingest python-multipart-free)."""
    title = _clean_title(title)
    cap = settings.raster_max_upload_mb * 1024 * 1024
    src_path, _src_size = await _spool_body(request, cap)
    cog_path: str | None = None
    try:
        # Inspect BEFORE writing anything durable — a bad/unsupported upload never touches the store/FGA/DB.
        try:
            info = await run_in_threadpool(inspect_raster, src_path)
        except NotACog as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        # COG is our internal storage/serve format. An already-COG upload is stored as-is; any other
        # accepted raster (plain GeoTIFF, JPEG 2000, ERDAS IMG) is converted to a COG here.
        if await run_in_threadpool(is_cog, src_path):
            cog_path = src_path
        else:
            fd, cog_path = tempfile.mkstemp(suffix=".cog.tif")
            os.close(fd)
            try:
                await run_in_threadpool(to_cog, src_path, cog_path)
            except Exception as exc:
                # Fixed text to the caller; GDAL's own message goes to the log only
                # (decision-once-over-security-fixes.md A1 — a crafted file's error text is a
                # channel back to whoever uploaded it).
                log.warning("raster_convert_failed", error=exc.__class__.__name__, detail=str(exc)[:300])
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="could not convert this raster to a COG.",
                )
        cog_size = os.path.getsize(cog_path)
        thumb = await run_in_threadpool(make_thumbnail_png, cog_path, info.default_render["bidx"])

        raster_id = uuid4()
        object_key = f"rasters/{raster_id}/source.tif"
        thumb_key = f"rasters/{raster_id}/thumb.png" if thumb else None

        # OpenFGA FIRST — the owner grant. A failure raises 503 and NOTHING is stored (no orphan item).
        await _openfga_write([(f"user:{user.id}", "owner", f"raster:{raster_id}")])
        try:
            await run_in_threadpool(put_file, object_key, cog_path, "image/tiff")
            if thumb:
                await run_in_threadpool(put_bytes, thumb_key, thumb, "image/png")
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO platform.rasters "
                    "(id, title, description, status, bucket, object_key, thumb_key, srid, extent, "
                    " band_count, dtype, width, height, pixel_size, size_bytes, default_render, "
                    " created_by, owner_id, updated_by) "
                    "VALUES (%s, %s, %s, 'ready', %s, %s, %s, %s, ST_GeomFromText(%s, 4326), "
                    " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING created_at, updated_at",
                    (str(raster_id), title, description, settings.raster_bucket, object_key, thumb_key,
                     info.srid, info.extent_wkt, info.band_count, info.dtype, info.width, info.height,
                     info.pixel_size, cog_size, Jsonb(info.default_render), user.id, user.id, user.id),
                )
                ts = await cur.fetchone()
            await conn.commit()
        except Exception:
            # Roll back durable state so no half-item survives: objects + owner tuple (best-effort).
            await conn.rollback()
            try:
                await run_in_threadpool(remove_prefix, f"rasters/{raster_id}/")
            except Exception:
                log.warning("raster_cleanup_objects_failed", raster=str(raster_id))
            try:
                await _openfga_delete(await _openfga_read_object_tuples(f"raster:{raster_id}"))
            except Exception:
                log.warning("raster_cleanup_tuple_failed", raster=str(raster_id))
            raise
    finally:
        _silent_unlink(src_path)
        if cog_path and cog_path != src_path:
            _silent_unlink(cog_path)

    log.info(
        "raster_created", raster=str(raster_id), actor=str(user.id),
        srid=info.srid, bands=info.band_count, bytes=cog_size,
        converted=(cog_path != src_path), thumbnail=bool(thumb),
    )
    return {
        "id": str(raster_id), "title": title, "description": description, "status": "ready",
        "srid": info.srid, "bandCount": info.band_count, "dtype": info.dtype,
        "width": info.width, "height": info.height, "pixelSize": info.pixel_size,
        "sizeBytes": cog_size, "hasThumbnail": bool(thumb), "defaultRender": info.default_render,
        "createdAt": ts[0], "updatedAt": ts[1],
    }


@router.get("/rasters/{raster_id}", summary="Raster item detail (can_view)")
async def get_raster(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Item metadata for the catalog/map. v1 authenticated-only: gated by can_view (403 if not).
    The public/anon seam (public_shared exists on the type) is a later step."""
    raster_id = _as_uuid(raster_id)
    if not await _openfga_check(user.id, "can_view", raster_id, object_type="raster"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to view this raster")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, title, description, status, srid, band_count, dtype, width, height, "
            "pixel_size, size_bytes, default_render, ST_AsGeoJSON(extent), thumb_key, "
            "created_at, updated_at, owner_id, downloadable, delete_protected "
            "FROM platform.rasters WHERE id = %s",
            (raster_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="raster not found")
    return {
        "id": str(row[0]), "title": row[1], "description": row[2], "status": row[3],
        "srid": row[4], "bandCount": row[5], "dtype": row[6], "width": row[7], "height": row[8],
        "pixelSize": row[9], "sizeBytes": row[10], "defaultRender": row[11],
        "extentGeoJSON": row[12], "hasThumbnail": row[13] is not None,
        "createdAt": row[14], "updatedAt": row[15], "ownerId": str(row[16]) if row[16] else None,
        "downloadable": row[17], "deleteProtected": row[18],
    }


# --- serve path (c6): dynamic COG tiles via rio-tiler, gated per object ----------
#
# A raster is immutable once uploaded (edit = re-upload/delete), so its tiles/thumbnail are safely
# cacheable — unlike vector tiles (which change on feature edits, so tiles.py uses no-cache). v1 is
# authenticated-only: every tile is can_view-gated (the public/anon seam is a later step).
_TILE_CACHE = "private, max-age=3600"


async def _load_serve_row(conn: AsyncConnection, raster_id: str) -> dict[str, Any]:
    """Load the serve-relevant row AFTER the caller has been gated. 404 if absent, 409 if not yet
    `ready` (the V045 lifecycle: only a ready raster has a stored, validated COG to serve)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT object_key, band_count, default_render, status, thumb_key, downloadable, title "
            "FROM platform.rasters WHERE id = %s",
            (raster_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="raster not found")
    if row[3] != "ready":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"raster is not ready (status={row[3]})")
    return {"id": raster_id, "object_key": row[0], "band_count": row[1],
            "default_render": row[2] or {}, "thumb_key": row[4],
            "downloadable": row[5], "title": row[6]}


async def _load_viewable(conn: AsyncConnection, user: User, raster_id: str) -> dict[str, Any]:
    """Gate can_view (403) + load the serve-relevant row — the authenticated-only loader."""
    raster_id = _as_uuid(raster_id)
    if not await _openfga_check(user.id, "can_view", raster_id, object_type="raster"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to view this raster")
    return await _load_serve_row(conn, raster_id)


async def _load_viewable_public(
    conn: AsyncConnection, principal: User | None, raster_id: str
) -> dict[str, Any]:
    """Optional-auth twin of _load_viewable (raster-public slice, decision-raster-public-sharing.md):
    an AUTHENTICATED caller keeps the exact can_view gate (403 on fail — unchanged semantics); an
    ANONYMOUS caller passes ONLY via the direct public_shared probe (never can_view), flat 404 on
    fail — existence-hiding, the T23 pattern. public_object_allowed fails CLOSED (503 propagates)."""
    raster_id = _as_uuid(raster_id)
    if principal is not None:
        if not await _openfga_check(principal.id, "can_view", raster_id, object_type="raster"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to view this raster")
    else:
        if not await public_object_allowed(conn, raster_id, object_type="raster"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="raster not found")
    return await _load_serve_row(conn, raster_id)


def _parse_bidx(raw: str | None, band_count: int, default: list[int]) -> list[int]:
    """Comma-separated 1-based band indexes, e.g. '1,2,3' or '1'. v1 renders 1 band (gray/colormap)
    or 3 (RGB). Out-of-range or wrong count -> 400."""
    if not raw:
        return default if default else [1]
    try:
        idx = [int(p) for p in raw.split(",") if p.strip() != ""]
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bidx must be comma-separated integers")
    if len(idx) not in (1, 3):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bidx must select 1 or 3 bands")
    if any(b < 1 or b > band_count for b in idx):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"bidx out of range 1..{band_count}")
    return idx


def _parse_rescale(raw: str | None, n: int) -> list[tuple[float, float]] | None:
    """Rescale ranges: 'min,max' (applied to every band) or 'min,max;min,max;...' (one per band)."""
    if not raw:
        return None
    out: list[tuple[float, float]] = []
    for part in raw.split(";"):
        bits = part.split(",")
        if len(bits) != 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="rescale must be 'min,max' or 'min,max;min,max;...'",
            )
        try:
            out.append((float(bits[0]), float(bits[1])))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="rescale values must be numbers")
    if len(out) == 1:
        out = out * n
    if len(out) != n:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"rescale must have 1 or {n} range(s) for {n} band(s)"
        )
    return out


def _validate_colormap(name: str | None, nbands: int) -> str | None:
    """A named rio-tiler colormap, valid ONLY for a single-band render. Unknown name / multi-band -> 400."""
    if not name:
        return None
    if nbands != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="colormap applies only to a single-band (1-band) render"
        )
    from rio_tiler.colormap import cmap

    if name not in cmap.list():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown colormap: {name}")
    return name


def _render_query(bidx: str | None, rescale: str | None, colormap: str | None) -> str:
    """Re-encode the validated render params into the tile-URL query so TileJSON tiles match the
    requested styling. Empty -> '' (defaults apply)."""
    from urllib.parse import urlencode

    params = [(k, v) for k, v in (("bidx", bidx), ("rescale", rescale), ("colormap", colormap)) if v]
    return f"?{urlencode(params)}" if params else ""


@router.get("/rasters/{raster_id}/tilejson.json",
            summary="TileJSON for a raster (can_view; anonymous if public)")
async def raster_tilejson(
    raster_id: str,
    request: Request,
    bidx: str | None = None,
    rescale: str | None = None,
    colormap: str | None = None,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    rec = await _load_viewable_public(conn, principal, raster_id)
    idx = _parse_bidx(bidx, rec["band_count"], rec["default_render"].get("bidx") or [1])
    _parse_rescale(rescale, len(idx))  # validate now so a bad TileJSON request fails fast
    _validate_colormap(colormap, len(idx))
    meta = await run_in_threadpool(tilejson_meta, rec["object_key"])
    base = str(request.base_url).rstrip("/")
    tile_url = f"{base}/api/v1/rasters/{rec['id']}/tiles/{{z}}/{{x}}/{{y}}.png{_render_query(bidx, rescale, colormap)}"
    return {
        "tilejson": "2.2.0",
        "name": rec["id"],
        "tiles": [tile_url],
        "minzoom": meta.minzoom,
        "maxzoom": meta.maxzoom,
        "bounds": meta.bounds,
        "center": meta.center,
    }


@router.get("/rasters/{raster_id}/tiles/{z}/{x}/{y}.png",
            summary="One raster tile (can_view; anonymous if public)")
async def raster_tile(
    raster_id: str,
    z: int,
    x: int,
    y: int,
    bidx: str | None = None,
    rescale: str | None = None,
    colormap: str | None = None,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> Response:
    rec = await _load_viewable_public(conn, principal, raster_id)
    idx = _parse_bidx(bidx, rec["band_count"], rec["default_render"].get("bidx") or [1])
    rng = _parse_rescale(rescale, len(idx))
    cmap_name = _validate_colormap(colormap, len(idx))
    try:
        png = await run_in_threadpool(render_tile, rec["object_key"], z, x, y, idx, rng, cmap_name)
    except MediaStoreUnavailable as exc:
        log.warning("raster_tile_store_unreachable", raster=rec["id"], detail=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="raster store unreachable")
    if png is None:  # tile outside the raster footprint — empty, like the vector proxy's 204
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": _TILE_CACHE})
    return Response(content=png, media_type="image/png", headers={"Cache-Control": _TILE_CACHE})


@router.get("/rasters/{raster_id}/info",
            summary="Raster bounds + zoom range for the map (can_view; anonymous if public)")
async def raster_serve_info(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    rec = await _load_viewable_public(conn, principal, raster_id)
    meta = await run_in_threadpool(read_thumbnail_or_tile_dims, rec["object_key"])
    return {**meta, "bandCount": rec["band_count"], "defaultRender": rec["default_render"]}


class RasterPoint(BaseModel):
    """The sampled band value(s) at a lon/lat, for the multi-layer identify popup. `values` is one entry
    per band (null = masked/nodata); `outOfBounds` = the click fell outside the raster's footprint."""
    values: list[float | None]
    bandCount: int
    outOfBounds: bool


@router.get("/rasters/{raster_id}/point",
            summary="Sample band value(s) at a lon/lat (can_view; anonymous if public)")
async def raster_point(
    raster_id: str,
    lon: float,
    lat: float,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> RasterPoint:
    """Read-only pixel inspection: the value(s) under a clicked point (rio-tiler windowed single-pixel
    read off the COG). can_view-gated + ready-gated like the tile/info routes; a display operation, NOT
    analytics (decision-raster-store-serve.md §9 fence). Anonymous on a PUBLIC raster follows the same
    line (raster-public fork 2): single-pixel inspection = viewing — deliberately NOT downloadable-
    gated, matching the signed-in behavior; bulk values (export/WCS) are the download class."""
    rec = await _load_viewable_public(conn, principal, raster_id)  # can_view/public + ready (409)
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="lon/lat outside valid ranges")
    try:
        values = await run_in_threadpool(read_point, rec["object_key"], lon, lat)
    except MediaStoreUnavailable as exc:
        log.warning("raster_point_store_unreachable", raster=rec["id"], detail=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="raster store unreachable")
    if values is None:
        return RasterPoint(values=[], bandCount=rec["band_count"], outOfBounds=True)
    return RasterPoint(values=values, bandCount=rec["band_count"], outOfBounds=False)


# --- export path (V050+): clip-and-download a GIS raster file, can_view + downloadable-gated ---------
#
# The raster analog of T33 Open Data's vector /export (decision-raster-export.md). A drawn bbox is a
# windowed read off the COG overviews (cheap), written to a real georeferenced file in the NATIVE CRS.
# Bounded by raster_max_export_megapixels: an over-cap window is REFUSED (413) unless &maxDim downsamples
# it. Authenticated-only in v1 (no anon raster); the path ends in /export so it inherits the anon
# `download` rate class if/when public raster lands.


def _parse_export_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    """'west,south,east,north' in EPSG:4326, or None for a whole-raster export. Malformed -> 400."""
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="bbox must be 'west,south,east,north' (EPSG:4326)"
        )
    try:
        w, s, e, n = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bbox values must be numbers")
    if not (-180.0 <= w <= 180.0 and -180.0 <= e <= 180.0 and -90.0 <= s <= 90.0 and -90.0 <= n <= 90.0):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bbox is outside valid lon/lat ranges")
    if w >= e or s >= n:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="bbox must have west<east and south<north"
        )
    return (w, s, e, n)


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _export_filename(title: str | None, raster_id: str, ext: str) -> str:
    """A friendly, filesystem-safe download name from the raster title (falls back to the id)."""
    base = _FILENAME_SAFE.sub("_", (title or "").strip()).strip("_")
    return f"{base or f'raster_{raster_id}'}.{ext}"


@router.get("/rasters/export/formats", summary="Writable export formats (probed server-side)")
async def raster_export_formats(
    user: User = Depends(get_current_user),
) -> dict[str, list[dict[str, str]]]:
    """The export formats THIS GDAL build can actually write — drives the UI format dropdown so it
    never offers a format the server would 400. (3-segment literal path; no clash with /rasters/{id}.)"""
    return {
        "formats": [
            {"key": s.key, "label": s.label, "ext": s.ext} for s in SUPPORTED_EXPORT_FORMATS.values()
        ]
    }


@router.get(
    "/rasters/{raster_id}/export",
    summary="Clip-and-download a raster (can_view + downloadable; anonymous if public); bbox optional = whole raster",
)
async def export_raster(
    raster_id: str,
    format: str = "cog",
    bbox: str | None = None,
    maxDim: int | None = None,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> FileResponse:
    # Anonymous on a PUBLIC raster is allowed with the SAME caps/formats as signed-in (raster-public
    # fork 4 — no anon-special knobs); the path ends in /export so the anon `download` rate class
    # applies automatically. downloadable gates EVERYONE (values/files are the download class).
    rec = await _load_viewable_public(conn, principal, raster_id)  # can_view/public + ready (409)
    if not rec["downloadable"]:
        log.info("authz_rejected", capability="rasters:export", reason="not_downloadable",
                 user_id=str(principal.id) if principal else "anon", raster=rec["id"])
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="this raster is marked view-only (not downloadable)"
        )
    spec = SUPPORTED_EXPORT_FORMATS.get(format.lower())
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported format '{format}'; available: {', '.join(sorted(SUPPORTED_EXPORT_FORMATS))}",
        )
    box = _parse_export_bbox(bbox)
    if maxDim is not None and (maxDim < 1 or maxDim > 20000):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="maxDim must be between 1 and 20000")
    try:
        path, native_w, native_h, downsampled = await run_in_threadpool(
            clip_to_file, rec["object_key"], box, spec, settings.raster_max_export_megapixels, maxDim
        )
    except ExportTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"selected area is {exc.native_width}x{exc.native_height} px at native resolution, over the "
                f"{exc.cap_megapixels} MP export cap — draw a smaller box or add &maxDim=<pixels> to downsample"
            ),
        )
    except ExportEmpty:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="the selected area does not overlap this raster",
        )
    except (MediaStoreUnavailable, MediaObjectMissing) as exc:
        log.warning("raster_export_store_unreachable", raster=rec["id"], detail=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="raster store unreachable")
    except Exception as exc:  # noqa: BLE001 — a GDAL write failure is a 500 with the temp cleaned up
        log.warning("raster_export_failed", raster=rec["id"], detail=str(exc))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="raster export failed")
    log.info("raster_exported", raster=rec["id"],
             actor=str(principal.id) if principal else "anon", format=spec.key,
             clipped=box is not None, native_px=native_w * native_h, downsampled=downsampled)
    return FileResponse(
        path,
        media_type=spec.media_type,
        filename=_export_filename(rec["title"], rec["id"], spec.ext),
        background=BackgroundTask(_silent_unlink, path),
    )


@router.get("/rasters/{raster_id}/thumbnail.png",
            summary="Raster catalog thumbnail (can_view; anonymous if public)")
async def raster_thumbnail(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> Response:
    rec = await _load_viewable_public(conn, principal, raster_id)
    if not rec["thumb_key"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no thumbnail for this raster")
    try:
        data = await run_in_threadpool(read_object, rec["thumb_key"])
    except MediaObjectMissing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thumbnail object missing")
    except MediaStoreUnavailable as exc:
        log.warning("raster_thumbnail_store_unreachable", raster=rec["id"], detail=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="raster store unreachable")
    return Response(content=data, media_type="image/png", headers={"Cache-Control": _TILE_CACHE})


# --- catalog: list / delete / share / transfer (c7) ------------------------------
#
# Mirrors web_maps catalog mechanics. Transfer reuses the generic ownership router
# (POST /catalog/raster/{id}/transfer — ownership.py registry). Delete-protection (T24 padlock) is
# DEFERRED: it needs a `delete_protected` column (its own migration, sequenced after the in-flight
# coded-domains migrations). Sharing is the full four-tier model: org + groups + PUBLIC
# (raster-public slice, decision-raster-public-sharing.md — the F-auth "later step" arrived; the
# publicAccess tier mirrors web_maps F5: independent of org/groups, F6-gated on turn-ON).


class RasterCatalogSummary(BaseModel):
    id: str
    title: str
    description: str | None
    # snake_case timestamps to match WebMapSummary — the frontend's shared catalog filter/sort keys
    # on `updated_at`.
    created_at: Any
    updated_at: Any
    srid: int | None
    bandCount: int | None
    hasThumbnail: bool
    # broadest share tier for the at-a-glance catalog icon: 'organization' | 'groups' | 'owner'
    # (None on an OpenFGA hiccup — the icon just doesn't render); favorite drives the ★ filter.
    shareLevel: str | None = None
    favorite: bool = False
    ownerId: str | None = None
    ownerName: str | None = None
    downloadable: bool = True
    deleteProtected: bool = False


class ShareGroup(BaseModel):
    group_id: str
    display_name: str


class RasterShares(BaseModel):
    """Raster sharing: org-wide + additive group shares + the anonymous Public Access tier
    (raster-public slice; an INDEPENDENT tier like web_maps F5, F6-gated on turn-ON)."""
    organizationOnly: bool = False
    groups: list[ShareGroup] = []
    publicAccess: bool = False


class RasterShareUpdate(BaseModel):
    organizationOnly: bool = False
    groups: list[str] = []
    publicAccess: bool = False


class RasterSettings(BaseModel):
    """Raster-specific download governance (V050): may /export serve this raster's pixels? Delete
    protection is the cross-item flag at /rasters/{id}/delete-protection (shared subsystem)."""
    downloadable: bool = True


class RasterDeleteProtection(BaseModel):
    deleteProtected: bool = False


async def _favorited_raster_ids(conn: AsyncConnection, user_id: Any) -> set[str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT item_id FROM platform.user_favorites WHERE user_id = %s AND item_type = 'raster'",
            (user_id,),
        )
        return {r[0] for r in await cur.fetchall()}


async def _raster_summaries(rows: list[Any], favs: set[str]) -> list[RasterCatalogSummary]:
    out: list[RasterCatalogSummary] = []
    for r in rows:
        try:
            shares = await get_object_shares(f"raster:{r[0]}")
            level = (
                "public" if shares["publicAccess"]
                else "organization" if shares["organizationOnly"]
                else "groups" if shares["groups"]
                else "owner"
            )
        except HTTPException:
            level = None
        out.append(RasterCatalogSummary(
            id=str(r[0]), title=r[1], description=r[2], created_at=r[3], updated_at=r[4],
            ownerId=str(r[5]) if r[5] else None, srid=r[6], bandCount=r[7],
            hasThumbnail=r[8] is not None, ownerName=r[9],
            shareLevel=level, favorite=str(r[0]) in favs,
            downloadable=r[10], deleteProtected=r[11],
        ))
    return out


_RASTER_SUMMARY_COLS = (
    "r.id, r.title, r.description, r.created_at, r.updated_at, r.owner_id, r.srid, r.band_count, "
    "r.thumb_key, COALESCE(u.display_name, u.username, u.email), r.downloadable, r.delete_protected "
    "FROM platform.rasters r LEFT JOIN platform.users u ON r.owner_id = u.id"
)


@router.get("/me/rasters", summary="The caller's rasters — owned + shared with them")
async def my_rasters(
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, list[RasterCatalogSummary]]:
    # Owned keys on owner_id (transferable, T26) — a cheap DB query, no per-row OpenFGA round-trip.
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_RASTER_SUMMARY_COLS} WHERE r.owner_id = %s AND r.status = 'ready' "
            "ORDER BY r.updated_at DESC",
            (user.id,),
        )
        owned_rows = await cur.fetchall()
    owned_ids = {str(r[0]) for r in owned_rows}

    # Shared with me: every raster the caller can_view, minus the ones they own. ListObjects fails
    # closed (503); here we DISPLAY a list, so degrade the shared bucket to empty on an OpenFGA blip
    # (fail-closed = shows LESS, never more) rather than 503-ing the whole catalog.
    try:
        viewable = await _openfga_list_objects(user.id, "can_view", "raster")
        shared_ids = [v for v in viewable if v not in owned_ids]
    except HTTPException:
        log.warning("rasters_shared_bucket_degraded", user_id=str(user.id))
        shared_ids = []

    shared_rows: list[Any] = []
    if shared_ids:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_RASTER_SUMMARY_COLS} WHERE r.id = ANY(%s) AND r.status = 'ready' "
                "ORDER BY r.updated_at DESC",
                (shared_ids,),
            )
            shared_rows = await cur.fetchall()

    favs = await _favorited_raster_ids(conn, user.id)
    return {
        "owned": await _raster_summaries(owned_rows, favs),
        "shared": await _raster_summaries(shared_rows, favs),
    }


async def _load_for_manage(
    conn: AsyncConnection, claims: dict[str, Any] | None, user: User, raster_id: str
) -> str:
    """Authorize manage (delete/share) of a raster: 404 if absent, else owner-OR-admin (else 403).
    Admin is role-only, checked FIRST so it short-circuits the OpenFGA call (mirrors web_maps)."""
    raster_id = _as_uuid(raster_id)
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.rasters WHERE id = %s", (raster_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="raster not found")
    if _has_admin_role(claims):
        return raster_id
    if await _openfga_check(user.id, "owner", raster_id, object_type="raster"):
        return raster_id
    log.info("authz_rejected", capability="rasters:manage", reason="not_owner_not_admin",
             user_id=str(user.id), raster=raster_id)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to modify this raster")


@router.delete("/rasters/{raster_id}", summary="Delete a raster (owner or admin)")
async def delete_raster(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    raster_id = await _load_for_manage(conn, claims, user, raster_id)
    # Delete-protection padlock (V050): a protected raster can't be deleted until the flag is cleared —
    # guards a load-bearing basemap against a stray click. ABSOLUTE: blocks ANYONE,
    # owner AND admin (AGO-faithful; guards human error incl. an admin's). No admin override — an admin
    # still holds the toggle, so it's a deliberate two-step (untick → delete), never a one-click force.
    async with conn.cursor() as cur:
        await cur.execute("SELECT delete_protected FROM platform.rasters WHERE id = %s", (raster_id,))
        prot = await cur.fetchone()
    if prot and prot[0]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="raster is delete-protected — turn off delete protection before deleting",
        )
    # OpenFGA FIRST (read every tuple -> delete), then the row + metadata + favorites, then PURGE the
    # stored objects. Order: the item leaves the catalog before its bytes go, so a purge failure leaves
    # harmless orphan cruft (logged), never a row pointing at missing objects.
    tuples = await _openfga_read_object_tuples(f"raster:{raster_id}")
    await _openfga_delete(tuples)
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM platform.rasters WHERE id = %s", (raster_id,))
        await cur.execute(
            "DELETE FROM platform.item_metadata WHERE item_type = 'raster' AND item_id = %s", (raster_id,)
        )
        await cur.execute(
            "DELETE FROM platform.user_favorites WHERE item_type = 'raster' AND item_id = %s", (raster_id,)
        )
    await conn.commit()
    try:
        await run_in_threadpool(remove_prefix, f"rasters/{raster_id}/")
    except Exception:
        log.warning("raster_delete_purge_failed", raster=raster_id)
    log.info("raster_deleted", raster=raster_id, actor=str(user.id), tuples_deleted=len(tuples))
    return {"id": raster_id, "status": "deleted"}


async def _raster_settings_response(conn: AsyncConnection, raster_id: str) -> RasterSettings:
    async with conn.cursor() as cur:
        await cur.execute("SELECT downloadable FROM platform.rasters WHERE id = %s", (raster_id,))
        row = await cur.fetchone()
    return RasterSettings(downloadable=row[0])


@router.get("/rasters/{raster_id}/settings", summary="Download governance flag (owner or admin)")
async def get_raster_settings(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> RasterSettings:
    raster_id = await _load_for_manage(conn, claims, user, raster_id)
    return await _raster_settings_response(conn, raster_id)


@router.put("/rasters/{raster_id}/settings", summary="Set the downloadable flag (owner or admin)")
async def set_raster_settings(
    raster_id: str,
    payload: RasterSettings,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> RasterSettings:
    raster_id = await _load_for_manage(conn, claims, user, raster_id)
    # Flag flip only — does NOT bump updated_at (toggling download availability isn't a data edit, so
    # it must not reorder the catalog's Modified sort; mirrors the delete-protection convention).
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.rasters SET downloadable = %s WHERE id = %s", (payload.downloadable, raster_id)
        )
    await conn.commit()
    log.info("raster_downloadable_set", raster=raster_id, actor=str(user.id), downloadable=payload.downloadable)
    return await _raster_settings_response(conn, raster_id)


# Delete-protection: the cross-item guard-rail subsystem (Talus 24), now extended to rasters via the
# V050 column. Same /{base}/{id}/delete-protection contract + {deleteProtected} shape as layers/maps,
# so the shared DeleteProtectionToggle component + catalog padlock work for rasters unchanged. The
# enforcement lives in delete_raster (blocks delete while set).


@router.get("/rasters/{raster_id}/delete-protection", summary="Delete-protection flag (owner or admin)")
async def get_raster_delete_protection(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> RasterDeleteProtection:
    raster_id = await _load_for_manage(conn, claims, user, raster_id)
    async with conn.cursor() as cur:
        await cur.execute("SELECT delete_protected FROM platform.rasters WHERE id = %s", (raster_id,))
        row = await cur.fetchone()
    return RasterDeleteProtection(deleteProtected=bool(row[0]))


@router.put("/rasters/{raster_id}/delete-protection", summary="Set delete-protection (owner or admin)")
async def set_raster_delete_protection(
    raster_id: str,
    payload: RasterDeleteProtection,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> RasterDeleteProtection:
    raster_id = await _load_for_manage(conn, claims, user, raster_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.rasters SET delete_protected = %s WHERE id = %s",
            (payload.deleteProtected, raster_id),
        )
    await conn.commit()
    log.info("raster_delete_protection_set", raster=raster_id, actor=str(user.id),
             protected=payload.deleteProtected)
    await admin_audit.record(
        conn, actor_id=user.id, action="raster_delete_protection_set", target_type="raster",
        target_id=raster_id, detail={"protected": payload.deleteProtected},
    )
    return RasterDeleteProtection(deleteProtected=payload.deleteProtected)


class RasterPublicShare(BaseModel):
    """A raster's Public Access state + whether THIS caller may change it — the single-tier path
    the web-map make-public cascade drives per referenced raster (mirrors layer_shares' T23 shape)."""
    publicAccess: bool = False
    canManage: bool = False


class RasterPublicShareUpdate(BaseModel):
    publicAccess: bool


async def _raster_manage_allowed(claims: dict[str, Any] | None, user: User, raster_id: str) -> bool:
    """The raster MANAGE key: talus-admin OR the OpenFGA owner (the same decision _load_for_manage
    enforces, minus the raising — the cascade needs it as a boolean)."""
    if _has_admin_role(claims):
        return True
    return await _openfga_check(user.id, "owner", raster_id, object_type="raster")


@router.get(
    "/rasters/{raster_id}/public-share",
    summary="A raster's Public Access state + whether the caller may change it",
)
async def get_raster_public_share(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> RasterPublicShare:
    # Public status isn't secret — the cascade review must list a map's rasters including ones the
    # caller may NOT manage — so this is open to any authenticated caller; the real gate is on PUT.
    raster_id = _as_uuid(raster_id)
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.rasters WHERE id = %s", (raster_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="raster not found")
    shares = await get_object_shares(f"raster:{raster_id}")
    return RasterPublicShare(
        publicAccess=shares["publicAccess"],
        canManage=await _raster_manage_allowed(claims, user, raster_id),
    )


@router.put(
    "/rasters/{raster_id}/public-share",
    summary="Set/clear a raster's Public Access (owner or admin)",
)
async def set_raster_public_share(
    raster_id: str,
    payload: RasterPublicShareUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> RasterPublicShare:
    raster_id = await _load_for_manage(conn, claims, user, raster_id)  # 404 / owner-or-admin 403
    # Gate only when turning public ON; un-publish (OFF) always allowed (reduces exposure).
    if payload.publicAccess:
        await assert_may_share_public(conn, claims)
    changed = await set_object_public(f"raster:{raster_id}", public=payload.publicAccess)
    if changed:
        log.info("object_public_changed", object=f"raster:{raster_id}", actor=str(user.id),
                 public=payload.publicAccess)
    return RasterPublicShare(publicAccess=payload.publicAccess, canManage=True)


async def _raster_shares_response(conn: AsyncConnection, raster_id: str) -> RasterShares:
    shares = await get_object_shares(f"raster:{raster_id}")
    slugs: list[str] = shares["groups"]
    names: dict[str, str] = {}
    if slugs:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_id, display_name FROM platform.groups WHERE group_id = ANY(%s)", (slugs,)
            )
            names = {r[0]: r[1] for r in await cur.fetchall()}
    return RasterShares(
        organizationOnly=shares["organizationOnly"],
        groups=[ShareGroup(group_id=s, display_name=names.get(s, s)) for s in slugs],
        publicAccess=shares["publicAccess"],
    )


@router.get("/rasters/{raster_id}/shares", summary="Current share state (owner or admin)")
async def get_raster_shares(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> RasterShares:
    raster_id = await _load_for_manage(conn, claims, user, raster_id)
    return await _raster_shares_response(conn, raster_id)


@router.put("/rasters/{raster_id}/shares", summary="Set/replace share state (owner or admin)")
async def set_raster_shares(
    raster_id: str,
    payload: RasterShareUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> RasterShares:
    raster_id = await _load_for_manage(conn, claims, user, raster_id)
    requested = list(dict.fromkeys(payload.groups))  # de-dupe, preserve order
    if requested:
        async with conn.cursor() as cur:
            await cur.execute("SELECT group_id FROM platform.groups WHERE group_id = ANY(%s)", (requested,))
            known = {r[0] for r in await cur.fetchall()}
        unknown = [g for g in requested if g not in known]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown group(s): {', '.join(sorted(unknown))}"
            )
        # Member-scoping (the real gate; admin may share to any group). A publisher can't surface a
        # raster to a group they're not in. _openfga_read_user_groups fails closed (503).
        if not _has_admin_role(claims):
            member_of = set(await _openfga_read_user_groups(user.id))
            forbidden = [g for g in requested if g not in member_of]
            if forbidden:
                log.info("authz_rejected", capability="rasters:share", reason="group_not_member",
                         user_id=str(user.id), raster=raster_id, groups=forbidden)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"not authorized to share to group(s): {', '.join(sorted(forbidden))}",
                )
    # Open Data public invariant (T33 §F8, uniform across item types): the public catalog enumerates
    # `group:open_data`, so a request adding it must ALSO turn Public Access ON — bound here so the
    # generic share path can't be used to bypass it.
    coupled = sorted(PUBLIC_SYSTEM_GROUPS & set(requested))
    if coupled and not payload.publicAccess:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"sharing to {', '.join(coupled)} requires public access",
        )
    # Open Data metadata gate (T33 §F8): nothing undocumented enters the public catalog. Removal is
    # never gated.
    if OPEN_DATA_GROUP in requested:
        complete, missing = await metadata_complete(conn, "raster", raster_id)
        if not complete:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "metadata_incomplete", "missing": missing},
            )

    # Turning Public Access ON is gated (the F6 deploy switch + org policy for non-admins); turning
    # it OFF only reduces exposure, so it's always allowed for this owner/admin. The org/group write
    # and the public write are INDEPENDENT (F5): set_object_shares never touches public_shared.
    if payload.publicAccess:
        await assert_may_share_public(conn, claims)
    await set_object_shares(f"raster:{raster_id}", organization_only=payload.organizationOnly, groups=requested)
    public_changed = await set_object_public(f"raster:{raster_id}", public=payload.publicAccess)
    log.info("raster_shares_set", raster=raster_id, actor=str(user.id),
             organization_only=payload.organizationOnly, groups=requested, public=payload.publicAccess)
    if public_changed:
        # A distinct, loud audit line for the irreversible-exposure transition (T23 §F3).
        log.info("object_public_changed", object=f"raster:{raster_id}", actor=str(user.id),
                 public=payload.publicAccess)
    return await _raster_shares_response(conn, raster_id)
